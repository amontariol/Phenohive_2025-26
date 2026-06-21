"""Store records locally and push them to InfluxDB when possible."""

from __future__ import annotations

import csv
import json
import logging
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)


class DataManager:
    """Handle CSV persistence, Influx writes, and offline retry queue."""

    def __init__(
        self,
        csv_path: str,
        influx_enabled: bool,
        influx_url: str,
        influx_token: str,
        influx_org: str,
        influx_bucket: str,
        offline_queue_path: str,
        timeout_ms: int = 5000,
    ) -> None:
        self._csv_path = Path(csv_path)
        self._offline_queue_path = Path(offline_queue_path)
        self._influx_enabled = influx_enabled
        self._influx_url = influx_url
        self._influx_token = influx_token
        self._influx_org = influx_org
        self._influx_bucket = influx_bucket
        self._timeout_ms = timeout_ms

        self._csv_path.parent.mkdir(parents=True, exist_ok=True)
        self._offline_queue_path.parent.mkdir(parents=True, exist_ok=True)

        self._influx_client: Any | None = None
        self._write_api: Any | None = None

        # Serialise all persistence so the main publish loop and the background
        # camera-capture thread cannot interleave CSV/Influx/queue writes.
        self._lock = threading.Lock()

    def persist_and_send(self, record: dict[str, Any]) -> None:
        """Always write to CSV, then try sending upstream."""
        with self._lock:
            self._append_to_csv(record)

            if not self._influx_enabled:
                return

            sent = self._send_record(record)
            if sent:
                self._flush_offline_queue()
            else:
                self._append_to_offline_queue(record)

    def _append_to_csv(self, record: dict[str, Any]) -> None:
        """Append one record to the local CSV, keeping a single stable header.

        The header is the union of every key ever written. Records produced by
        the station do not all share the same schema — the camera thread writes
        ``vision_*`` records, calibration fields appear only periodically, and a
        failed sensor drops its fields for that cycle. A naive per-row header
        therefore drifts out of alignment and silently corrupts the archive.

        To prevent that, when a record introduces a previously unseen key the
        file is rewritten (atomically, via a temp file) with the expanded header
        and the existing rows back-filled with empty values. In steady state
        (no new keys) this is a plain append, so the common path stays cheap.
        """
        try:
            record_with_timestamp = dict(record)
            record_with_timestamp.setdefault("timestamp", datetime.now(UTC).isoformat())

            existing_fieldnames = self._read_csv_header()
            if existing_fieldnames is None:
                fieldnames = self._ordered_fieldnames(record_with_timestamp.keys())
                self._write_csv_rows(fieldnames, [record_with_timestamp], mode="w")
                return

            new_keys = [key for key in record_with_timestamp if key not in existing_fieldnames]
            if new_keys:
                fieldnames = existing_fieldnames + sorted(new_keys)
                self._rewrite_csv_with_fieldnames(fieldnames)
                self._write_csv_rows(fieldnames, [record_with_timestamp], mode="a")
            else:
                self._write_csv_rows(existing_fieldnames, [record_with_timestamp], mode="a")
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("Failed to write CSV record: %s", exc)

    @staticmethod
    def _ordered_fieldnames(keys: Any) -> list[str]:
        """Return fieldnames with ``timestamp`` first, then the rest sorted."""
        keys = list(keys)
        rest = sorted(key for key in keys if key != "timestamp")
        return (["timestamp"] if "timestamp" in keys else []) + rest

    def _read_csv_header(self) -> list[str] | None:
        """Return the current CSV header, or None if the file is empty/missing."""
        if not self._csv_path.exists() or self._csv_path.stat().st_size == 0:
            return None
        with self._csv_path.open("r", newline="", encoding="utf-8") as csv_file:
            reader = csv.reader(csv_file)
            try:
                return next(reader)
            except StopIteration:
                return None

    def _write_csv_rows(self, fieldnames: list[str], rows: list[dict[str, Any]], mode: str) -> None:
        """Write rows aligned to ``fieldnames``; missing keys become empty cells."""
        with self._csv_path.open(mode, newline="", encoding="utf-8") as csv_file:
            writer = csv.DictWriter(csv_file, fieldnames=fieldnames, extrasaction="ignore", restval="")
            if mode == "w":
                writer.writeheader()
            writer.writerows(rows)

    def _rewrite_csv_with_fieldnames(self, fieldnames: list[str]) -> None:
        """Rewrite the CSV under an expanded header, back-filling existing rows.

        Uses a temp file plus an atomic replace so an interruption mid-rewrite
        can never truncate or half-write the authoritative archive.
        """
        with self._csv_path.open("r", newline="", encoding="utf-8") as csv_file:
            existing_rows = list(csv.DictReader(csv_file))

        tmp_path = self._csv_path.with_name(self._csv_path.name + ".tmp")
        with tmp_path.open("w", newline="", encoding="utf-8") as tmp_file:
            writer = csv.DictWriter(tmp_file, fieldnames=fieldnames, extrasaction="ignore", restval="")
            writer.writeheader()
            writer.writerows(existing_rows)
        tmp_path.replace(self._csv_path)

    def _append_to_offline_queue(self, record: dict[str, Any]) -> None:
        """Append failed records to a JSONL queue."""
        try:
            with self._offline_queue_path.open("a", encoding="utf-8") as queue_file:
                queue_file.write(json.dumps(record, ensure_ascii=True) + "\n")
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("Failed to append record to offline queue: %s", exc)

    def _flush_offline_queue(self) -> None:
        """Retry queued records and keep only the ones that still fail."""
        if not self._offline_queue_path.exists() or self._offline_queue_path.stat().st_size == 0:
            return

        unsent_records: list[dict[str, Any]] = []
        try:
            with self._offline_queue_path.open("r", encoding="utf-8") as queue_file:
                for line in queue_file:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        queued_record = json.loads(line)
                    except json.JSONDecodeError:
                        LOGGER.warning("Skipping malformed offline queue record")
                        continue

                    if not self._send_record(queued_record):
                        unsent_records.append(queued_record)

            with self._offline_queue_path.open("w", encoding="utf-8") as queue_file:
                for unsent in unsent_records:
                    queue_file.write(json.dumps(unsent, ensure_ascii=True) + "\n")
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("Failed while flushing offline queue: %s", exc)

    def _ensure_influx_client(self) -> bool:
        """Create the Influx client on first use."""
        if self._write_api is not None:
            return True

        try:
            influx_module = __import__("influxdb_client")
            client_cls = getattr(influx_module, "InfluxDBClient")
            write_api_module = __import__(
                "influxdb_client.client.write_api",
                fromlist=["SYNCHRONOUS"],
            )
            synchronous = getattr(write_api_module, "SYNCHRONOUS")

            self._influx_client = client_cls(
                url=self._influx_url,
                token=self._influx_token,
                org=self._influx_org,
                timeout=self._timeout_ms,
            )
            self._write_api = self._influx_client.write_api(write_options=synchronous)
            return True
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("InfluxDB client initialization failed: %s", exc)
            self._write_api = None
            self._influx_client = None
            return False

    def _send_record(self, record: dict[str, Any]) -> bool:
        """Send one flattened record to InfluxDB."""
        if not self._ensure_influx_client():
            return False

        try:
            influx_module = __import__("influxdb_client")
            point_cls = getattr(influx_module, "Point")
            point = point_cls("phenohive_measurements")

            timestamp = record.get("timestamp", datetime.now(UTC).isoformat())
            for key, value in record.items():
                if key == "timestamp":
                    continue

                # Keep only stable labels as tags to avoid one-point-per-series cardinality.
                if key in ["station_id", "hardware_uuid"]:
                    point = point.tag(key, str(value))
                elif isinstance(value, bool):
                    point = point.field(key, 1.0 if value else 0.0)
                elif isinstance(value, (int, float)):
                    point = point.field(key, float(value))
                elif value is None:
                    continue
                elif isinstance(value, str) and key.endswith("_status"):
                    # Write status as a field, not a tag. Tags are part of the InfluxDB series
                    # key — writing transient status strings as tags fragments each metric into
                    # a new series every time the status changes, causing Grafana to show
                    # multiple lines for the same sensor.
                    point = point.field(key, value)
                else:
                    # Drop high-cardinality free-form strings (UTC markers, diagnostic text, etc.)
                    # from InfluxDB points. They remain available in local CSV storage.
                    continue

            point = point.time(timestamp)
            self._write_api.write(bucket=self._influx_bucket, org=self._influx_org, record=point)
            return True
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("InfluxDB write failed: %s", exc)
            return False
