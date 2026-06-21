"""Create sensor instances (real or mock) from config."""

from __future__ import annotations

import logging
import time
from typing import Dict

from .config_manager import ConfigManager
from ..sensors import (
    BaseSensor,
    MockScaleHX711,
    MockSHT35,
    MockTCS3448,
    RealScaleHX711,
    RealSHT35,
    RealTCS3448,
    SensorStatus,
)

LOGGER = logging.getLogger(__name__)


class SensorFactory:
    """Build sensor objects from configuration."""

    def __init__(self, config: ConfigManager) -> None:
        self._config = config
        self._i2c_addresses: dict[str, tuple[int, int]] = {}

    def create_all(self) -> Dict[str, BaseSensor]:
        """Instantiate all supported sensors."""
        sensors: Dict[str, BaseSensor] = {
            "sht35": self._create_sht35(),
            "tcs3448": self._create_tcs3448(),
            "scale_hx711": self._create_scale_hx711(),
        }
        return sensors

    # Sensors that share the I2C bus must be set up one at a time; concurrent
    # access from two threads causes the BCM2835 I2C controller to emit EIO
    # during the other sensor's transaction even though the kernel serialises
    # individual ioctl calls.  HX711 is GPIO-only so it runs in parallel.
    _I2C_SETUP_ORDER = ("tcs3448", "sht35")  # TCS3448 first: its init is longer

    def setup_all(self, sensors: Dict[str, BaseSensor]) -> Dict[str, bool]:
        """Run setup for sensors, serialising I2C bus sensors to avoid contention."""
        import concurrent.futures

        results: Dict[str, bool] = {key: False for key in sensors}
        timeout_s = 30.0  # extended: I2C sensors now run sequentially

        i2c_keys = [k for k in self._I2C_SETUP_ORDER if k in sensors]
        other_keys = [k for k in sensors if k not in i2c_keys]

        # Non-I2C sensors run in a background thread (they don't touch the bus)
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(other_keys))) as executor:
            other_futures = {executor.submit(sensors[k].setup): k for k in other_keys}

            # I2C sensors run strictly sequentially on the calling thread
            for key in i2c_keys:
                try:
                    results[key] = sensors[key].setup()
                except Exception as exc:
                    LOGGER.error("Setup failed for sensor %s: %s", key, exc)
                    results[key] = False

            done, not_done = concurrent.futures.wait(
                other_futures.keys(),
                timeout=timeout_s,
                return_when=concurrent.futures.ALL_COMPLETED,
            )
            for future in done:
                key = other_futures[future]
                try:
                    results[key] = future.result()
                except Exception as exc:
                    LOGGER.error("Setup failed for sensor %s: %s", key, exc)
                    results[key] = False
            for future in not_done:
                key = other_futures[future]
                LOGGER.error("Setup timed out after %ss for sensor %s", timeout_s, key)
                results[key] = False

        return results

    def retry_failed_sensors(
        self,
        sensors: Dict[str, BaseSensor],
        max_retries: int = 3,
        retry_delay_s: float = 2.0,
    ) -> Dict[str, bool]:
        """Retry setup for I2C sensors in error state that are still detectable on the bus.

        Runs i2c_bus_recovery.sh before attempting setup so that a stuck bus or
        sensor in a bad post-crash state is cleared before each retry round.

        Returns a dict of {sensor_key: success} for every sensor that was retried.
        Sensors that are not I2C or are already healthy are silently skipped.
        """
        failed_i2c = {
            key for key, sensor in sensors.items()
            if sensor.status != SensorStatus.READY and key in self._i2c_addresses
        }
        if failed_i2c:
            LOGGER.info("Running I2C bus recovery before retrying sensors: %s", failed_i2c)
            self._run_i2c_recovery()

        # Retry I2C sensors in setup order (TCS3448 first) so that if TCS3448's
        # failed init corrupts the bus, SHT35 doesn't exhaust retries on a broken
        # bus before TCS3448 gets a chance to recover and clean it up.
        i2c_retry_order = list(self._I2C_SETUP_ORDER) + [
            k for k in sensors if k not in self._I2C_SETUP_ORDER
        ]
        results: Dict[str, bool] = {}
        for key in i2c_retry_order:
            sensor = sensors.get(key)
            if sensor is None or sensor.status == SensorStatus.READY:
                continue
            i2c_info = self._i2c_addresses.get(key)
            if i2c_info is None:
                continue
            bus, address = i2c_info
            probe_ok = self._probe_i2c(bus, address)
            if not probe_ok:
                # Probe can fail even for physically-present sensors when the bus is
                # in a partially-broken state after a failed TCS3448 init sequence.
                # Log the result but do not skip — let setup() decide if the sensor
                # is truly absent.
                LOGGER.warning(
                    "Sensor %s in %s state; I2C probe failed at bus %d 0x%02X — retrying anyway",
                    key, sensor.status.value, bus, address,
                )
            else:
                LOGGER.info(
                    "Sensor %s detected at I2C bus %d 0x%02X, retrying setup (max %d attempt(s))",
                    key, bus, address, max_retries,
                )
            for attempt in range(1, max_retries + 1):
                if attempt > 1:
                    # A failed setup attempt can leave the bus in a bad state
                    # (partial init write strands the BCM2835 I2C controller).
                    # Re-run bus recovery before each subsequent attempt.
                    LOGGER.info("Re-running I2C recovery before retry %d/%d for %s", attempt, max_retries, key)
                    self._run_i2c_recovery()
                    if retry_delay_s > 0:
                        time.sleep(retry_delay_s)
                if sensor.setup():
                    LOGGER.info("Sensor %s recovered on setup attempt %d/%d", key, attempt, max_retries)
                    results[key] = True
                    break
                LOGGER.warning("Sensor %s setup retry %d/%d failed", key, attempt, max_retries)
            else:
                results[key] = False
        return results

    def _run_i2c_recovery(self) -> None:
        """Run the i2c_bus_recovery.sh script to clear a stuck bus or sensor state."""
        import subprocess
        from pathlib import Path
        script = Path(__file__).resolve().parent.parent.parent / "scripts" / "i2c_bus_recovery.sh"
        if not script.exists():
            LOGGER.warning("I2C recovery script not found at %s — skipping", script)
            return
        try:
            result = subprocess.run(
                ["bash", str(script)],
                timeout=15,
                capture_output=True,
                text=True,
            )
            if result.stdout:
                for line in result.stdout.strip().splitlines():
                    LOGGER.info("[i2c_recovery] %s", line)
            if result.returncode != 0 and result.stderr:
                LOGGER.warning("[i2c_recovery] script exited %d: %s", result.returncode, result.stderr.strip())
            time.sleep(1.0)
        except Exception as exc:
            LOGGER.warning("I2C recovery script failed: %s", exc)

    @staticmethod
    def _probe_i2c(bus: int, address: int) -> bool:
        """Return True if a device ACKs at the given I2C address.

        Uses write_quick (same as i2cdetect) instead of read_byte because some
        sensors (e.g. SHT35) NACK naked reads, causing false negatives.
        """
        try:
            import smbus2
            with smbus2.SMBus(bus) as b:
                b.write_quick(address)
            return True
        except OSError:
            return False
        except Exception:
            return False

    def _create_sht35(self) -> BaseSensor:
        temp_offset = self._config.get_float("sht35", "temp_offset", fallback=0.0)
        hum_offset = self._config.get_float("sht35", "hum_offset", fallback=0.0)
        if self._config.use_mock_for_sensor("sht35"):
            return MockSHT35(temp_offset=temp_offset, hum_offset=hum_offset)
        addr = self._parse_int("sht35", "i2c_address", 0x44)
        bus = self._config.get_int("sht35", "i2c_bus", fallback=1)
        self._i2c_addresses["sht35"] = (bus, addr)
        return RealSHT35(i2c_address=addr, temp_offset=temp_offset, hum_offset=hum_offset)


    def _create_tcs3448(self) -> BaseSensor:
        dark_offset = self._config.get_int("tcs3448", "dark_offset", fallback=0)
        channels = ['f1', 'f2', 'f3', 'f4', 'f5', 'f6', 'f7', 'f8', 'fz', 'fy', 'fxl', 'nir', '2x_vis_1', 'fd_1']
        scaling = {ch: self._config.get_float("tcs3448", f"scale_{ch}", fallback=1.0) for ch in channels}
        offsets = {ch: self._config.get_int("tcs3448", f"offset_{ch}", fallback=dark_offset) for ch in channels}

        if self._config.use_mock_for_sensor("tcs3448"):
            return MockTCS3448(dark_offset=dark_offset, channel_scaling=scaling, channel_offsets=offsets)
        addr = self._parse_int("tcs3448", "i2c_address", 0x29)
        bus = self._config.get_int("tcs3448", "i2c_bus", fallback=1)
        self._i2c_addresses["tcs3448"] = (bus, addr)
        return RealTCS3448(
            i2c_address=addr,
            integration_time=self._config.get_int("tcs3448", "integration_time", fallback=50),
            gain=self._config.get_int("tcs3448", "gain", fallback=4),
            dark_offset=dark_offset,
            channel_scaling=scaling,
            channel_offsets=offsets
        )

    def _create_scale_hx711(self) -> BaseSensor:
        if self._config.use_mock_for_sensor("scale_hx711"):
            return MockScaleHX711(
                samples=self._config.get_int("scale_hx711", "samples", fallback=10),
                tare=self._config.get_float("scale_hx711", "tare", fallback=0.0),
                calibration_factor=self._config.get_float("scale_hx711", "calibration_factor", fallback=1.0),
                weight_offset=self._config.get_float("scale_hx711", "weight_offset", fallback=0.0),
            )
        return RealScaleHX711(
            dout_pin=self._config.get_int("scale_hx711", "dout_pin", fallback=5),
            pd_sck_pin=self._config.get_int("scale_hx711", "pd_sck_pin", fallback=6),
            samples=self._config.get_int("scale_hx711", "samples", fallback=10),
            tare=self._config.get_float("scale_hx711", "tare", fallback=0.0),
            calibration_factor=self._config.get_float("scale_hx711", "calibration_factor", fallback=1.0),
            weight_offset=self._config.get_float("scale_hx711", "weight_offset", fallback=0.0),
        )

    def _parse_int(self, section: str, option: str, default: int) -> int:
        """Parse int values from config (decimal or hex)."""
        raw_value = self._config.get_str(section, option, fallback=str(default))
        if raw_value is None:
            return default
        try:
            return int(raw_value, 0)
        except ValueError:
            return default
