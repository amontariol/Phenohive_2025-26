"""Keep app timestamps aligned with NTP, with safe fallbacks."""

from __future__ import annotations

import logging
import socket
import struct
import time
from datetime import UTC, datetime, timedelta
from typing import Iterable

LOGGER = logging.getLogger(__name__)

_NTP_EPOCH_TO_UNIX = 2208988800


class TimeSyncService:
    """Maintain a UTC offset from NTP without touching the system clock."""

    def __init__(
        self,
        enabled: bool = True,
        servers: Iterable[str] | None = None,
        timeout_s: float = 2.0,
        resync_interval_s: int = 86400,
    ) -> None:
        self._enabled = enabled
        self._servers = list(servers or ["pool.ntp.org", "time.google.com", "time.cloudflare.com"])
        self._timeout_s = max(0.2, timeout_s)
        self._resync_interval_s = max(300, resync_interval_s)

        self._offset_s = 0.0
        self._last_sync_at: datetime | None = None
        self._last_server: str | None = None
        self._last_error: str | None = None

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def resync_interval_s(self) -> int:
        return self._resync_interval_s

    @property
    def last_sync_at(self) -> datetime | None:
        return self._last_sync_at

    @property
    def offset_s(self) -> float:
        return self._offset_s

    @property
    def last_server(self) -> str | None:
        return self._last_server

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def now_utc(self) -> datetime:
        """Return current UTC time with the sync offset applied."""
        return datetime.now(UTC) + timedelta(seconds=self._offset_s)

    def is_resync_due(self) -> bool:
        """Return whether it's time to sync again."""
        if not self._enabled:
            return False
        if self._last_sync_at is None:
            return True
        return (datetime.now(UTC) - self._last_sync_at).total_seconds() >= self._resync_interval_s

    def sync_now(self) -> bool:
        """Try to sync against configured NTP servers."""
        if not self._enabled:
            self._offset_s = 0.0
            self._last_sync_at = datetime.now(UTC)
            self._last_server = None
            self._last_error = None
            return True

        for server in self._servers:
            try:
                ntp_utc = self._query_ntp_utc(server=server, timeout_s=self._timeout_s)
                local_utc = datetime.now(UTC)
                self._offset_s = (ntp_utc - local_utc).total_seconds()
                self._last_sync_at = datetime.now(UTC)
                self._last_server = server
                self._last_error = None
                LOGGER.info("Time sync OK via %s (offset=%.3fs)", server, self._offset_s)
                return True
            except Exception as exc:  # noqa: BLE001
                self._last_error = str(exc)
                LOGGER.warning("Time sync failed via %s: %s", server, exc)

        LOGGER.warning("All NTP servers failed. Continuing with local clock.")
        return False

    def diagnostics(self) -> dict[str, str | float | bool | None]:
        """Return sync state for logs and the debug UI."""
        return {
            "enabled": self._enabled,
            "offset_s": round(self._offset_s, 6),
            "last_sync_at": self._last_sync_at.isoformat() if self._last_sync_at else None,
            "last_server": self._last_server,
            "last_error": self._last_error,
            "resync_interval_s": self._resync_interval_s,
        }

    @staticmethod
    def _query_ntp_utc(server: str, timeout_s: float) -> datetime:
        """Query a single NTP server and return UTC time."""
        packet = b"\x1b" + 47 * b"\0"
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout_s)
            sock.sendto(packet, (server, 123))
            data, _ = sock.recvfrom(48)

        if len(data) < 48:
            raise RuntimeError("Invalid NTP response length")

        seconds, fraction = struct.unpack("!II", data[40:48])
        unix_seconds = seconds - _NTP_EPOCH_TO_UNIX
        frac_seconds = fraction / 2**32
        return datetime.fromtimestamp(unix_seconds + frac_seconds, tz=UTC)


def build_time_sync_from_config(config: object) -> TimeSyncService:
    """Build a `TimeSyncService` from config-like getters."""
    enabled = getattr(config, "get_bool")("time_sync", "enabled", fallback=True)
    timeout_s = getattr(config, "get_float")("time_sync", "timeout_s", fallback=2.0)
    interval_h = getattr(config, "get_int")("time_sync", "interval_hours", fallback=24)
    servers_raw = getattr(config, "get_str")(
        "time_sync",
        "servers",
        fallback="pool.ntp.org,time.google.com,time.cloudflare.com",
    )
    servers = [item.strip() for item in (servers_raw or "").split(",") if item.strip()]

    return TimeSyncService(
        enabled=enabled,
        servers=servers,
        timeout_s=timeout_s,
        resync_interval_s=max(1, interval_h) * 3600,
    )
