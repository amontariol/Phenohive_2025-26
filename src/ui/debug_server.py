"""Small debug web UI for runtime and config inspection.

Serves the dashboard, status/config APIs, and the captured-image gallery.
"""

from __future__ import annotations

import configparser
import hashlib
import hmac
import html
import json
import logging
import os
import socket
import sys
import threading
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

LOGGER = logging.getLogger("phenohive")
TEMPLATE_PATH = Path(__file__).with_name("debug_dashboard.html")

NON_EDITABLE_OPTION_TOKENS = {
    "dout",
    "i2c",
    "pd_sck",
    "pin",
    "token",
    "password",
    "secret",
    "ssid",
}

NON_EDITABLE_OPTIONS_EXACT: set[tuple[str, str]] = {
    ("influxdb", "url"),
    ("paths", "csv_path"),
    ("paths", "offline_queue_path"),
    ("paths", "log_dir"),
    ("debug_ui", "enabled"),
    ("debug_ui", "host"),
    ("debug_ui", "port"),
    ("debug_ui", "write_token"),
    ("debug_ui", "allow_remote_writes"),
}


class DualStackServer(ThreadingHTTPServer):
    """HTTP server using IPv4."""

    address_family = socket.AF_INET


class DebugUIService:
    """Serve a local debug page with status and config views."""

    def __init__(
        self,
        config_path: Path,
        get_status: Callable[[], dict[str, Any]],
        write_token: str = "",
        ta_password: str = "",
        allow_remote_writes: bool = False,
        image_dir: Path | None = None,
        camera_service: Any = None,
        image_processor: Any = None,
        sensors: dict[str, BaseSensor] | None = None,
        config_validator: Callable[[str, str, str], str | None] | None = None,
    ) -> None:
        self._config_path = config_path
        self._defaults_path = config_path.with_name("config.defaults.ini")
        self._get_status = get_status
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._write_token = write_token.strip()
        self._ta_password = ta_password.strip()
        self._allow_remote_writes = allow_remote_writes
        self._image_dir = image_dir or (config_path.parent / "data" / "images")
        self._camera_service = camera_service
        self._image_processor = image_processor
        self._sensors = sensors or {}
        self._validator = config_validator
        self._manual_dir = config_path.parent / "docs"

    def start(self, host: str = "0.0.0.0", port: int = 8080) -> None:
        """Start the debug HTTP server in a daemon thread."""
        if self._server is not None:
            return

        service = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                LOGGER.info("DEBUG_SERVER: GET request to %s", self.path)
                if self.path == "/":
                    status = service._get_status()
                    self._send_html(service._render_dashboard(status))
                    return
                if self.path.startswith("/images/"):
                    # Strip any query string (e.g. cache-busting ?t=123) before
                    # resolving the file on disk.
                    requested = self.path[len("/images/"):].split("?", 1)[0]
                    self._serve_image(requested)
                    return
                if self.path.startswith("/manual/"):
                    filename = self.path[8:]
                    if ".." not in filename and filename:
                        manual_path = service._manual_dir / filename
                        if manual_path.exists() and manual_path.is_file():
                            content = manual_path.read_text(encoding="utf-8").encode("utf-8")
                            self.send_response(HTTPStatus.OK)
                            self.send_header("Content-Type", "text/plain; charset=utf-8")
                            self.send_header("Content-Length", str(len(content)))
                            self.end_headers()
                            self.wfile.write(content)
                            return
                    self.send_error(HTTPStatus.NOT_FOUND, "Manual not found")
                    return
                if self.path == "/api/status":
                    self._send_json(service._get_status())
                    return
                if self.path == "/api/config":
                    self._send_json(service._load_config_dict(mask_sensitive=True))
                    return
                if self.path == "/api/config/editable":
                    self._send_json(service._load_editable_config_dict())
                    return
                if self.path == "/api/images":
                    self._send_json(service._list_capture_images())
                    return
                self.send_error(HTTPStatus.NOT_FOUND, "Not Found")

            def do_POST(self) -> None:  # noqa: N802
                print(f"STDOUT_DEBUG: POST to {self.path}")
                LOGGER.info("DEBUG_SERVER: POST request to %s", self.path)
                if self.path == "/api/auth":
                    try:
                        content_length = int(self.headers.get("Content-Length", "0"))
                        body = self.rfile.read(content_length) if content_length > 0 else b"{}"
                        payload = json.loads(body.decode("utf-8"))
                        password = str(payload.get("password", "")).strip()
                        if not service._ta_password:
                            self._send_json({"ok": True})
                        elif hmac.compare_digest(password, service._ta_password):
                            self._send_json({"ok": True})
                        else:
                            self._send_json({"ok": False, "error": "Incorrect password"}, status=HTTPStatus.UNAUTHORIZED)
                    except Exception as exc:  # noqa: BLE001
                        self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                    return
                if self.path == "/api/config":
                    try:
                        allowed, reason = service._is_write_request_authorized(self)
                        if not allowed:
                            self._send_json({"ok": False, "error": reason}, status=HTTPStatus.FORBIDDEN)
                            return

                        content_length = int(self.headers.get("Content-Length", "0"))
                        body = self.rfile.read(content_length) if content_length > 0 else b"{}"
                        payload = json.loads(body.decode("utf-8"))
                        section = str(payload["section"]).strip()
                        option = str(payload["option"]).strip()
                        value = str(payload["value"]).strip()
                        if not service._is_editable_option(section=section, option=option):
                            raise ValueError(f"Option [{section}] {option} is not editable from Debug UI")
                        
                        # Use validator if available
                        LOGGER.info("DEBUG_SERVER: API /api/config called with %s.%s = %s", section, option, value)
                        if service._validator:
                            LOGGER.info("DEBUG_SERVER: Calling validator %s", service._validator)
                            error_msg = service._validator(section, option, value)
                            LOGGER.info("DEBUG_SERVER: Validator returned: %s", error_msg)
                            if error_msg:
                                self._send_json({"ok": False, "error": error_msg}, status=HTTPStatus.BAD_REQUEST)
                                return

                        service._update_config_option(section=section, option=option, value=value)
                        self._send_json({"ok": True, "message": "Config updated. Restart application to apply."})
                    except Exception as exc:  # noqa: BLE001
                        self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                    return

                if self.path == "/api/config/reset-defaults":
                    try:
                        allowed, reason = service._is_write_request_authorized(self)
                        if not allowed:
                            self._send_json({"ok": False, "error": reason}, status=HTTPStatus.FORBIDDEN)
                            return

                        content_length = int(self.headers.get("Content-Length", "0"))
                        body = self.rfile.read(content_length) if content_length > 0 else b"{}"
                        payload = json.loads(body.decode("utf-8"))
                        requested_sections = payload.get("sections", []) if isinstance(payload, dict) else []
                        sections_filter = {
                            str(section).strip().lower()
                            for section in requested_sections
                            if str(section).strip()
                        }
                        updated_count = service._reset_editable_config_to_defaults(
                            sections=sections_filter if sections_filter else None
                        )
                        self._send_json(
                            {
                                "ok": True,
                                "updated": updated_count,
                                "message": "Editable settings reset to defaults. Restart application to apply.",
                            }
                        )
                    except Exception as exc:  # noqa: BLE001
                        self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                    return

                if self.path == "/api/camera/capture-background":
                    try:
                        allowed, reason = service._is_write_request_authorized(self)
                        if not allowed:
                            self._send_json({"ok": False, "error": reason}, status=HTTPStatus.FORBIDDEN)
                            return

                        if not service._camera_service or not service._camera_service.is_ready:
                            self._send_json({"ok": False, "error": "Camera service not available"}, status=HTTPStatus.SERVICE_UNAVAILABLE)
                            return

                        bg_path = service._image_dir / "background.jpg"
                        captured = service._camera_service.capture_file(bg_path, warmup_seconds=7.0)
                        if not captured:
                            self._send_json({"ok": False, "error": "Capture failed"}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
                            return

                        self._send_json({"ok": True, "message": "Background image captured and saved."})
                    except Exception as exc:  # noqa: BLE001
                        self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                    return

                if self.path == "/api/restart":
                    try:
                        allowed, reason = service._is_write_request_authorized(self)
                        if not allowed:
                            self._send_json({"ok": False, "error": reason}, status=HTTPStatus.FORBIDDEN)
                            return

                        LOGGER.warning("Restart requested via Debug UI")
                        self._send_json({"ok": True, "message": "Restarting application..."})
                        
                        # Use a timer to exit after sending the response
                        # Use os._exit to kill the entire process immediately
                        threading.Timer(0.5, lambda: os._exit(0)).start()
                    except Exception as exc:  # noqa: BLE001
                        self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                    return

                self.send_error(HTTPStatus.NOT_FOUND, "Not Found")

            def log_message(self, fmt: str, *args: Any) -> None:
                LOGGER.debug("DebugUI: " + fmt, *args)

            def _send_html(self, body: str, status: HTTPStatus = HTTPStatus.OK) -> None:
                payload = body.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
                body = json.dumps(payload, ensure_ascii=True, indent=2).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _serve_image(self, filename: str) -> None:
                """Serve an image file from the image directory."""
                if ".." in filename or filename.startswith(("/", "\\")):
                    self.send_error(HTTPStatus.FORBIDDEN, "Access denied")
                    return
                img_path = service._image_dir / filename
                if not img_path.exists() or not img_path.is_file():
                    self.send_error(HTTPStatus.NOT_FOUND, "Image not found")
                    return
                if img_path.suffix.lower() not in {".jpg", ".jpeg", ".png"}:
                    self.send_error(HTTPStatus.BAD_REQUEST, "Invalid image type")
                    return

                try:
                    content = img_path.read_bytes()
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "image/jpeg")
                    self.send_header("Content-Length", str(len(content)))
                    self.send_header("Cache-Control", "public, max-age=3600")
                    self.end_headers()
                    self.wfile.write(content)
                except Exception as exc:  # noqa: BLE001
                    self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

        self._server = DualStackServer((host, port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        LOGGER.info("Debug UI [MARKER-V123] available at http://%s:%s", host if host else "0.0.0.0", port)

    def stop(self) -> None:
        """Stop the debug server if it is running."""
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        self._server = None
        self._thread = None

    def _load_config_dict(self, mask_sensitive: bool) -> dict[str, dict[str, str]]:
        parser = configparser.ConfigParser()
        parser.optionxform = str.lower
        parser.read(self._config_path)

        output: dict[str, dict[str, str]] = {}
        for section in parser.sections():
            output[section] = {}
            for key, value in parser.items(section):
                if mask_sensitive and any(token in key.lower() for token in ("token", "password", "secret")):
                    output[section][key] = "***"
                else:
                    output[section][key] = value
        return output

    def _is_editable_option(self, section: str, option: str) -> bool:
        section_name = section.strip().lower()
        option_name = option.strip().lower()
        if not section_name or not option_name:
            return False
        if (section_name, option_name) in NON_EDITABLE_OPTIONS_EXACT:
            return False
        return not any(token in option_name for token in NON_EDITABLE_OPTION_TOKENS)

    def _is_write_request_authorized(self, request_handler: BaseHTTPRequestHandler) -> tuple[bool, str]:
        client_host = request_handler.client_address[0] if request_handler.client_address else ""
        is_local_client = client_host in {"127.0.0.1", "::1", "localhost"}

        if not self._allow_remote_writes and not is_local_client:
            return False, "Remote config writes are disabled"

        if not self._write_token:
            return True, ""

        auth_header = request_handler.headers.get("Authorization", "")
        expected = f"Bearer {self._write_token}"
        if not hmac.compare_digest(auth_header, expected):
            return False, "Missing or invalid debug write token"

        return True, ""

    def _load_editable_config_dict(self) -> dict[str, dict[str, str]]:
        config = self._load_config_dict(mask_sensitive=False)
        editable: dict[str, dict[str, str]] = {}
        for section, options in config.items():
            filtered_options = {
                option: value
                for option, value in options.items()
                if self._is_editable_option(section=section, option=option)
            }
            if filtered_options:
                editable[section] = filtered_options
        return editable

    def _load_default_editable_config_dict(self) -> dict[str, dict[str, str]]:
        parser = configparser.ConfigParser()
        parser.optionxform = str.lower

        if self._defaults_path.exists():
            parser.read(self._defaults_path)
        else:
            parser.read(self._config_path)

        editable: dict[str, dict[str, str]] = {}
        for section in parser.sections():
            filtered_options = {
                option: value
                for option, value in parser.items(section)
                if self._is_editable_option(section=section, option=option)
            }
            if filtered_options:
                editable[section] = filtered_options
        return editable

    def _reset_editable_config_to_defaults(self, sections: set[str] | None = None) -> int:
        with self._lock:
            current_parser = configparser.ConfigParser()
            current_parser.optionxform = str.lower
            current_parser.read(self._config_path)

            defaults_parser = configparser.ConfigParser()
            defaults_parser.optionxform = str.lower
            if self._defaults_path.exists():
                defaults_parser.read(self._defaults_path)
            else:
                defaults_parser.read(self._config_path)

            updated_count = 0
            for section in defaults_parser.sections():
                if sections is not None and section.lower() not in sections:
                    continue
                if not current_parser.has_section(section):
                    current_parser.add_section(section)

                for option, value in defaults_parser.items(section):
                    if not self._is_editable_option(section=section, option=option):
                        continue
                    current_parser.set(section, option, value)
                    updated_count += 1

            with self._config_path.open("w", encoding="utf-8") as config_file:
                current_parser.write(config_file)

            return updated_count

    def _update_config_option(self, section: str, option: str, value: str) -> None:
        with self._lock:
            parser = configparser.ConfigParser()
            parser.optionxform = str.lower
            parser.read(self._config_path)
            if not parser.has_section(section):
                parser.add_section(section)
            parser.set(section, option, value)

            with self._config_path.open("w", encoding="utf-8") as config_file:
                parser.write(config_file)

            # Hot-reload ta_password immediately so login uses the new value without restart
            if section == "debug_ui" and option == "ta_password":
                self._ta_password = value.strip()

            # Hot-reload if it's a sensor setting
            if section in self._sensors:
                try:
                    self._sensors[section].update_setting(option, value)
                except Exception:
                    LOGGER.exception("Failed to hot-reload setting %s.%s", section, option)

    def _format_capture_age(self, status: dict[str, Any]) -> str:
        latest_cycle = status.get("latest_cycle", {})
        last_cycle_utc = latest_cycle.get("last_cycle_utc") if isinstance(latest_cycle, dict) else None
        if not last_cycle_utc:
            return "No capture yet"

        try:
            parsed = datetime.fromisoformat(str(last_cycle_utc).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            age_seconds = max(0, int((datetime.now(UTC) - parsed).total_seconds()))
        except ValueError:
            return f"Last capture: {last_cycle_utc}"

        minutes, seconds = divmod(age_seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if hours > 0:
            return f"{hours}h {minutes}m {seconds}s ago"
        if minutes > 0:
            return f"{minutes}m {seconds}s ago"
        return f"{seconds}s ago"

    def _format_time_until_next_capture(self, status: dict[str, Any]) -> str:
        latest_cycle = status.get("latest_cycle", {})
        last_cycle_utc = latest_cycle.get("last_cycle_utc") if isinstance(latest_cycle, dict) else None
        if not last_cycle_utc:
            return "Waiting for first capture"

        interval_raw = status.get("measurement_interval_s")
        try:
            interval_seconds = max(1, int(interval_raw))
        except (TypeError, ValueError):
            return "Unavailable"

        try:
            parsed = datetime.fromisoformat(str(last_cycle_utc).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            elapsed_seconds = max(0, int((datetime.now(UTC) - parsed).total_seconds()))
        except ValueError:
            return "Unavailable"

        remaining_seconds = max(0, interval_seconds - elapsed_seconds)
        minutes, seconds = divmod(remaining_seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if hours > 0:
            return f"{hours}h {minutes}m {seconds}s"
        if minutes > 0:
            return f"{minutes}m {seconds}s"
        return f"{seconds}s"

    @staticmethod
    def _parse_capture_timestamp(stem: str) -> datetime | None:
        """Parse a capture filename stem (e.g. 2026-06-20T14-30-00Z) to a UTC datetime."""
        try:
            parsed = datetime.strptime(stem, "%Y-%m-%dT%H-%M-%SZ")
        except ValueError:
            return None
        return parsed.replace(tzinfo=UTC)

    def _list_capture_images(self) -> dict[str, Any]:
        """List capture images in the image directory, newest first.

        Returns the timestamped captures (parsed from their filenames), the latest
        capture, and the background image if present. Non-capture files such as
        skeleton.jpg are ignored because their stem does not parse as a timestamp.
        """
        background: dict[str, Any] | None = None
        rows: list[tuple[datetime, str, str, str, float]] = []

        try:
            entries = list(self._image_dir.iterdir()) if self._image_dir.exists() else []
        except OSError:
            LOGGER.exception("Unable to read image directory %s", self._image_dir)
            entries = []

        for entry in entries:
            try:
                if not entry.is_file():
                    continue
                if entry.suffix.lower() not in {".jpg", ".jpeg", ".png"}:
                    continue
                try:
                    mtime = entry.stat().st_mtime
                except OSError:
                    mtime = 0.0
                if entry.stem.lower() == "background":
                    background = {"filename": entry.name, "mtime": mtime}
                    continue
                parsed = self._parse_capture_timestamp(entry.stem)
                if parsed is None:
                    continue
                rows.append(
                    (
                        parsed,
                        entry.name,
                        parsed.isoformat().replace("+00:00", "Z"),
                        parsed.strftime("%Y-%m-%d %H:%M:%S UTC"),
                        mtime,
                    )
                )
            except OSError:
                continue

        # Newest capture first so the dropdown and "last capture" reflect recency.
        rows.sort(key=lambda row: row[0], reverse=True)
        captures = [
            {"filename": name, "iso": iso, "label": label, "mtime": mtime}
            for (_, name, iso, label, mtime) in rows
        ]
        return {
            "captures": captures,
            "latest": captures[0] if captures else None,
            "background": background,
            "count": len(captures),
        }

    def _render_dashboard(self, status: dict[str, Any]) -> str:
        config_json = json.dumps(self._load_config_dict(mask_sensitive=True), ensure_ascii=True, indent=2)
        status_json = json.dumps(status, ensure_ascii=True, indent=2)
        editable_config = self._load_editable_config_dict()
        editable_json = html.escape(json.dumps(editable_config, ensure_ascii=True, indent=2))
        editable_json_raw = json.dumps(editable_config, ensure_ascii=True)
        default_editable_json_raw = json.dumps(self._load_default_editable_config_dict(), ensure_ascii=True)
        recent_captures = status.get("recent_captures", [])
        recent_captures_json = html.escape(json.dumps(recent_captures, ensure_ascii=True, indent=2))
        capture_age_text = html.escape(self._format_capture_age(status))
        next_capture_text = html.escape(self._format_time_until_next_capture(status))
        template = TEMPLATE_PATH.read_text(encoding="utf-8")
        rendered = template.replace("{{UTC_NOW}}", datetime.now(UTC).isoformat())
        rendered = rendered.replace("{{STATUS_JSON}}", status_json)
        rendered = rendered.replace("{{CONFIG_JSON}}", config_json)
        rendered = rendered.replace("{{EDITABLE_CONFIG_JSON}}", editable_json)
        rendered = rendered.replace("{{EDITABLE_CONFIG_JSON_RAW}}", editable_json_raw)
        rendered = rendered.replace("{{DEFAULT_EDITABLE_CONFIG_JSON_RAW}}", default_editable_json_raw)
        rendered = rendered.replace("{{CAPTURE_AGE_TEXT}}", capture_age_text)
        rendered = rendered.replace("{{NEXT_CAPTURE_TEXT}}", next_capture_text)
        rendered = rendered.replace("{{RECENT_CAPTURES_JSON}}", recent_captures_json)
        rendered = rendered.replace("{{HAS_TA_PASSWORD}}", "1" if self._ta_password else "")
        return rendered
