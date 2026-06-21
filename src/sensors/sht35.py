"""SHT35 sensor implementations (real hardware + mock)."""

from __future__ import annotations

import logging
import random
from datetime import UTC, datetime
from importlib import import_module
from typing import Any

from .base_sensor import BaseSensor, SensorStatus

LOGGER = logging.getLogger(__name__)


class RealSHT35(BaseSensor):
    """Read temperature and humidity from an SHT35 over I2C."""

    def __init__(self, i2c_address: int = 0x45, temp_offset: float = 0.0, hum_offset: float = 0.0) -> None:
        super().__init__(name="sht35")
        self._i2c_address = i2c_address
        self._temp_offset = temp_offset
        self._hum_offset = hum_offset
        self._sensor: Any | None = None

    def setup(self) -> bool:
        """Initialize the sensor and run a quick probe."""
        import time
        # Two attempts: adafruit's I2C probe can fail while the sensor is still
        # recovering from a soft-reset (bus recovery script) even though it ACKs
        # the address on a quick-read probe. Retry once after a short delay.
        for attempt in range(2):
            try:
                board_module = import_module("board")
                sht_module = import_module("adafruit_sht31d")
                sht_cls = getattr(sht_module, "SHT31D")

                i2c = board_module.I2C()
                self._sensor = sht_cls(i2c, address=self._i2c_address)
                self.mark_ready()
                LOGGER.info("SHT35 initialized at I2C address 0x%02X (attempt %d)", self._i2c_address, attempt + 1)
                return True
            except ValueError as exc:
                if attempt == 0:
                    LOGGER.warning("SHT35 probe failed on first attempt (%s), retrying in 2s", exc)
                    time.sleep(2.0)
                    continue
                msg = f"SHT35 setup failed: {exc}"
                self.mark_error(msg)
                LOGGER.error(msg)
                return False
            except Exception as exc:  # noqa: BLE001 - hardware backends raise different errors
                msg = f"SHT35 setup failed: {exc}"
                self.mark_error(msg)
                LOGGER.exception(msg)
                return False
        return False

    def read_data(self) -> dict[str, Any]:
        """Read one temperature/humidity sample."""
        timestamp = datetime.now(UTC).isoformat()
        if self.status != SensorStatus.READY:
            return {"sensor": self.name, "timestamp": timestamp, "status": self.status.value, "error": self.last_error}
        if self._sensor is None:
            msg = "SHT35 read attempted before successful setup"
            self.mark_error(msg)
            LOGGER.error(msg)
            return {
                "sensor": self.name,
                "timestamp": timestamp,
                "status": self.status.value,
                "error": self.last_error,
            }

        try:
            air_temp_c = float(self._sensor.temperature)
            rel_humidity_pct = float(self._sensor.relative_humidity)
            
            cal_temp = air_temp_c + self._temp_offset
            cal_hum = rel_humidity_pct + self._hum_offset
            
            self.mark_ready()
            return {
                "sensor": self.name,
                "timestamp": timestamp,
                "status": self.status.value,
                "raw_temperature_c": round(air_temp_c, 2),
                "raw_humidity_pct": round(rel_humidity_pct, 2),
                "air_temperature_c": round(cal_temp, 2),
                "air_humidity_pct": round(cal_hum, 2),
            }
        except Exception as exc:  # noqa: BLE001 - hardware backends raise different errors
            msg = f"SHT35 read failed: {exc}"
            self.mark_error(msg)
            LOGGER.exception(msg)
            return {
                "sensor": self.name,
                "timestamp": timestamp,
                "status": self.status.value,
                "error": self.last_error,
            }


class MockSHT35(BaseSensor):
    """Mock SHT35 with gently changing synthetic values."""

    def __init__(
        self,
        seed: int | None = None,
        start_temp_c: float = 23.4,
        start_humidity_pct: float = 57.0,
        temp_offset: float = 0.0,
        hum_offset: float = 0.0,
    ) -> None:
        super().__init__(name="sht35")
        self._random = random.Random(seed)
        self._temp_c = start_temp_c
        self._humidity_pct = start_humidity_pct
        self._temp_offset = temp_offset
        self._hum_offset = hum_offset

    def setup(self) -> bool:
        """Initialize mock state."""
        self.mark_ready()
        LOGGER.info("Mock SHT35 initialized")
        return True

    def read_data(self) -> dict[str, Any]:
        """Generate one synthetic temperature/humidity sample."""
        timestamp = datetime.now(UTC).isoformat()
        try:
            self._temp_c += self._random.uniform(-0.20, 0.20)
            self._temp_c = max(10.0, min(40.0, self._temp_c))

            self._humidity_pct += self._random.uniform(-0.8, 0.8)
            self._humidity_pct = max(15.0, min(95.0, self._humidity_pct))

            cal_temp = self._temp_c + self._temp_offset
            cal_hum = self._humidity_pct + self._hum_offset

            self.mark_ready()
            return {
                "sensor": self.name,
                "timestamp": timestamp,
                "status": self.status.value,
                "raw_temperature_c": round(self._temp_c, 2),
                "raw_humidity_pct": round(self._humidity_pct, 2),
                "air_temperature_c": round(cal_temp, 2),
                "air_humidity_pct": round(cal_hum, 2),
            }
        except Exception as exc:  # noqa: BLE001 - should not happen, but keep mock resilient
            msg = f"Mock SHT35 read failed unexpectedly: {exc}"
            self.mark_error(msg)
            LOGGER.exception(msg)
            return {
                "sensor": self.name,
                "timestamp": timestamp,
                "status": self.status.value,
                "error": self.last_error,
            }
