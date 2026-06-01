"""HX711 scale sensor implementations (real hardware + mock)."""

from __future__ import annotations

import logging
import random
import statistics
from datetime import UTC, datetime
from importlib import import_module
from typing import Any

from .base_sensor import BaseSensor

LOGGER = logging.getLogger(__name__)


class RealScaleHX711(BaseSensor):
    """HX711-backed load cell reader."""

    def __init__(
        self,
        dout_pin: int = 5,
        pd_sck_pin: int = 6,
        samples: int = 10,
        tare: float = 0.0,
        calibration_factor: float = 1.0,
        weight_offset: float = 0.0,
    ) -> None:
        super().__init__(name="scale_hx711")
        self._dout_pin = dout_pin
        self._pd_sck_pin = pd_sck_pin
        self._samples = max(1, samples)
        self._tare = tare
        self._calibration_factor = calibration_factor
        self._weight_offset = weight_offset
        self._hx: Any | None = None
        self._last_raw: float | None = None

    def setup(self) -> bool:
        """Initialize and sanity-check the HX711."""
        try:
            hx_module = import_module("hx711")
            hx_cls = getattr(hx_module, "HX711")

            self._hx = hx_cls(dout_pin=self._dout_pin, pd_sck_pin=self._pd_sck_pin)
            self._hx.reset()

            self.mark_ready()
            LOGGER.info("HX711 initialized on pins DOUT=%s PD_SCK=%s", self._dout_pin, self._pd_sck_pin)
            return True
        except Exception as exc:  # noqa: BLE001 - backend exceptions differ by hardware stack
            msg = f"HX711 setup failed: {exc}"
            self.mark_error(msg)
            LOGGER.exception(msg)
            return False

    def read_data(self) -> dict[str, Any]:
        """Read samples and return calibrated weight data."""
        timestamp = datetime.now(UTC).isoformat()
        if self._hx is None:
            msg = "HX711 read attempted before successful setup"
            self.mark_error(msg)
            LOGGER.error(msg)
            return {
                "sensor": self.name,
                "timestamp": timestamp,
                "status": self.status.value,
                "error": self.last_error,
            }

        try:
            samples = self._hx.get_raw_data(times=self._samples)
            if not samples:
                raise RuntimeError("HX711 returned no samples")

            raw_median = float(statistics.median(samples))
            raw_stddev = float(statistics.stdev(samples)) if len(samples) > 1 else 0.0
            net_raw = raw_median - self._tare
            weight_g = net_raw * self._calibration_factor - self._weight_offset

            self.mark_ready()
            self._last_raw = raw_median
            return {
                "sensor": self.name,
                "timestamp": timestamp,
                "status": self.status.value,
                "weight_raw": round(raw_median, 4),  # Actual raw ADC value for calibration
                "weight_g": round(weight_g, 4),      # Calibrated weight (taking offset into account)
                "weight_stddev_raw": round(raw_stddev, 4),
                "sample_count": len(samples),
            }
        except Exception as exc:  # noqa: BLE001 - backend exceptions differ by hardware stack
            msg = f"HX711 read failed: {exc}"
            self.mark_error(msg)
            LOGGER.exception(msg)
            return {
                "sensor": self.name,
                "timestamp": timestamp,
                "status": self.status.value,
                "error": self.last_error,
            }

    def update_setting(self, key: str, value: Any) -> None:
        """Update tare, calibration factor, or weight offset at runtime."""
        if key == "tare":
            self._tare = float(value)
            LOGGER.info("HX711 tare updated to %s", self._tare)
        elif key == "calibration_factor":
            self._calibration_factor = float(value)
            LOGGER.info("HX711 calibration factor updated to %s", self._calibration_factor)
        elif key == "weight_offset":
            self._weight_offset = float(value)
            LOGGER.info("HX711 weight offset updated to %s g", self._weight_offset)


class MockScaleHX711(BaseSensor):
    """Mock scale that simulates realistic weight changes."""

    def __init__(
        self,
        seed: int | None = None,
        start_weight_g: float = 350.0,
        noise_std_g: float = 1.4,
        samples: int = 10,
        tare: float = 0.0,
        calibration_factor: float = 1.0,
        weight_offset: float = 0.0,
    ) -> None:
        super().__init__(name="scale_hx711")
        self._random = random.Random(seed)
        self._base_weight_g = start_weight_g
        self._noise_std_g = max(0.01, noise_std_g)
        self._samples = max(1, samples)
        self._tare = tare
        self._calibration_factor = calibration_factor
        self._weight_offset = weight_offset

    def setup(self) -> bool:
        """Initialize mock state."""
        self.mark_ready()
        LOGGER.info("Mock HX711 initialized")
        return True

    def read_data(self) -> dict[str, Any]:
        """Generate synthetic samples and return aggregate values."""
        timestamp = datetime.now(UTC).isoformat()
        try:
            # Small drift over time to mimic watering/evaporation cycles.
            self._base_weight_g += self._random.uniform(-0.8, 0.5)
            self._base_weight_g = max(100.0, min(1200.0, self._base_weight_g))

            samples = [
                self._random.gauss(self._base_weight_g, self._noise_std_g)
                for _ in range(self._samples)
            ]
            median_g = float(statistics.median(samples))
            stddev_g = float(statistics.stdev(samples)) if len(samples) > 1 else 0.0

            # Simulate raw value that would produce this weight given the current config
            # weight_g = (raw - tare) * calibration_factor - weight_offset
            # => raw = ((weight_g + weight_offset) / calibration_factor) + tare
            simulated_raw = ((median_g + self._weight_offset) / self._calibration_factor) + self._tare
            reported_g = median_g - self._weight_offset

            self.mark_ready()
            return {
                "sensor": self.name,
                "timestamp": timestamp,
                "status": self.status.value,
                "weight_raw": round(simulated_raw, 4),
                "weight_g": round(reported_g, 4),
                "weight_stddev_raw": round(stddev_g, 4),
                "sample_count": len(samples),
            }
        except Exception as exc:  # noqa: BLE001 - unexpected but keep mock resilient
            msg = f"Mock HX711 read failed unexpectedly: {exc}"
            self.mark_error(msg)
            LOGGER.exception(msg)
            return {
                "sensor": self.name,
                "timestamp": timestamp,
                "status": self.status.value,
                "error": self.last_error,
            }

    def update_setting(self, key: str, value: Any) -> None:
        """Update tare, calibration factor, or weight offset at runtime."""
        if key == "tare":
            self._tare = float(value)
        elif key == "calibration_factor":
            self._calibration_factor = float(value)
        elif key == "weight_offset":
            self._weight_offset = float(value)
