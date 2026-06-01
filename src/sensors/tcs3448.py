"""TCS3448 light sensor implementations (real hardware + mock)."""

from __future__ import annotations

import logging
import random
from datetime import UTC, datetime
from importlib import import_module
from typing import Any

from .base_sensor import BaseSensor

LOGGER = logging.getLogger(__name__)


class RealTCS3448(BaseSensor):
    """Read 14-channel spectral data from a TCS3448/TCS3408 (Color 21 Click)."""

    def __init__(
        self,
        i2c_address: int = 0x59,
        integration_time: int = 50,
        gain: int = 4,
        dark_offset: int = 0,
        channel_scaling: dict[str, float] | None = None,
        channel_offsets: dict[str, int] | None = None,
    ) -> None:
        super().__init__(name="tcs3448")
        self._i2c_address = i2c_address
        self._integration_time = integration_time
        self._gain = gain
        self._dark_offset = dark_offset
        self._channel_scaling = channel_scaling or {}
        self._channel_offsets = channel_offsets or {}
        self._bus: Any | None = None

    # ------------------------------------------------------------------
    # Raw I2C helpers (use I2C_RDWR, not I2C_SMBUS — the TCS3408 does
    # not respond correctly to the SMBus ioctl on BCM2835).
    # ------------------------------------------------------------------
    def _i2c_read_reg(self, reg: int) -> int:
        import smbus2
        w = smbus2.i2c_msg.write(self._i2c_address, [reg])
        r = smbus2.i2c_msg.read(self._i2c_address, 1)
        self._bus.i2c_rdwr(w, r)
        return list(r)[0]

    def _i2c_write_reg(self, reg: int, val: int) -> None:
        import smbus2
        self._bus.i2c_rdwr(smbus2.i2c_msg.write(self._i2c_address, [reg, val]))

    def _i2c_write_block(self, reg: int, data: list) -> None:
        import smbus2
        self._bus.i2c_rdwr(smbus2.i2c_msg.write(self._i2c_address, [reg] + data))

    def _open_bus(self) -> None:
        """Close any stale bus handle and open a fresh one."""
        import smbus2
        if self._bus is not None:
            try:
                self._bus.close()
            except Exception:
                pass
            self._bus = None
        self._bus = smbus2.SMBus(1)

    def _run_init_sequence(self) -> None:
        """Run the full TCS3448 hardware initialisation (raises on failure)."""
        import time

        # Blind power-off then power-on: recovers the chip if a previous process was
        # killed mid-SMUX, or if the recovery script already powered it off.
        # Register 0xBF (bank select) is only accessible when PON=1.
        try:
            self._i2c_write_reg(0x80, 0x00)
            time.sleep(0.05)
            self._i2c_write_reg(0x80, 0x01)
            time.sleep(0.05)
        except OSError:
            pass

        # 1. Bank 1 access
        cfg_0 = self._i2c_read_reg(0xBF)
        self._i2c_write_reg(0xBF, cfg_0 | 0x10)

        # 2. Check ID
        device_id = self._i2c_read_reg(0x5A)
        LOGGER.info("TCS3448 detected ID: 0x%02X", device_id)

        # 3. Bank 0 access
        cfg_0 = self._i2c_read_reg(0xBF)
        self._i2c_write_reg(0xBF, cfg_0 & ~0x10)

        # 4. Init sequence (Power off, Power on, Reset)
        self._i2c_write_reg(0x80, 0x00)
        time.sleep(0.1)
        self._i2c_write_reg(0x80, 0x01)
        time.sleep(0.1)
        self._i2c_write_reg(0xFA, 0x08)
        time.sleep(0.2)
        # 0xFA may act as a soft-reset that clears PON; re-assert it so that
        # subsequent writes to PON-gated registers (ASTEP) don't fail with EIO.
        self._i2c_write_reg(0x80, 0x01)
        time.sleep(0.05)

        # 5. Enable LED (Default 8mA = 0x82)
        self._i2c_write_reg(0xCD, 0x82)

        # 6. ATIME and ASTEP
        # ASTEP is split into two single-byte writes to avoid clock-stretch
        # timeout on the BCM2835 I2C controller during the 3-byte block write.
        atime_val = max(0, min(255, self._integration_time))
        self._i2c_write_reg(0x81, atime_val)
        self._i2c_write_reg(0xD4, 0xE7)  # ASTEP_L
        self._i2c_write_reg(0xD5, 0x03)  # ASTEP_H

        # 7. WTIME
        self._i2c_write_reg(0x83, 35)

        # 8. Gain (CFG_1)
        gain_reg = max(0, min(12, self._gain))
        self._i2c_write_reg(0xC6, gain_reg)

        # 9. Clear SMUX and configure for 14-channel
        self._i2c_write_reg(0xAF, 0x10)  # SMUX Command: Write
        smux_map = [
            0x00, 0x00, 0x00, 0x40, 0x02, 0x11, 0x04, 0x05,
            0x21, 0x03, 0x33, 0x03, 0x03, 0x33, 0x00, 0x00,
            0x00, 0x00, 0x00, 0x00
        ]
        self._i2c_write_block(0x85, smux_map)
        self._i2c_write_reg(0xAF, 0x11)  # Execute SMUX
        time.sleep(0.05)

        # 10. Enable WEN, ALS_EN, PON and Auto SMUX
        self._i2c_write_reg(0x80, 0x0B)
        self._i2c_write_reg(0xD6, 0x60)

        # Verify ENABLE was actually stored — a marginal I2C bus can ACK the
        # write byte without committing it, leaving AEN=0 and AVALID stuck low.
        actual_enable = self._i2c_read_reg(0x80)
        if actual_enable != 0x0B:
            raise OSError(
                f"ENABLE readback failed (wrote 0x0B, read 0x{actual_enable:02X}) — "
                "possible silent I2C write loss during bus recovery"
            )

    def setup(self) -> bool:
        """Initialize the custom TCS3448 sequence."""
        import time
        # Two attempts: on the first I2C-level error (EIO/EREMOTEIO) reopen the
        # bus and wait for the I2C controller to recover before trying again.
        for attempt in range(2):
            try:
                self._open_bus()
                if attempt > 0:
                    time.sleep(1.0)
                self._run_init_sequence()
                self.mark_ready()
                LOGGER.info("TCS3448 14-Channel initialization complete at 0x%02X (attempt %d)", self._i2c_address, attempt + 1)
                return True
            except OSError as exc:
                if attempt == 0 and getattr(exc, "errno", None) in (5, 121):
                    LOGGER.warning("TCS3448 I2C error on first attempt (%s), reopening bus and retrying", exc)
                    continue
                msg = f"TCS3448 setup failed: {exc}"
                self.mark_error(msg)
                LOGGER.exception(msg)
                return False
            except Exception as exc:
                msg = f"TCS3448 setup failed: {exc}"
                self.mark_error(msg)
                LOGGER.exception(msg)
                return False
        return False

    def read_data(self) -> dict[str, Any]:
        """Read 14-channel spectral sample."""
        timestamp = datetime.now(UTC).isoformat()
        if self._bus is None:
            msg = "TCS3448 read attempted before successful setup"
            self.mark_error(msg)
            LOGGER.error(msg)
            return {"sensor": self.name, "timestamp": timestamp, "status": self.status.value, "error": msg}

        try:
            import smbus2

            # Check status2 (0x90) for AVALID (0x40)
            status2 = self._i2c_read_reg(0x90)
            if not (status2 & 0x40):
                # Data not ready yet
                return {"sensor": self.name, "timestamp": timestamp, "status": self.status.value, "error": "Data not ready"}

            # Read 38 bytes starting from STATUS (0x93)
            write = smbus2.i2c_msg.write(self._i2c_address, [0x93])
            read = smbus2.i2c_msg.read(self._i2c_address, 38)
            self._bus.i2c_rdwr(write, read)
            data_buf = list(read)

            # Parse the channels according to C struct
            ch_fz = (data_buf[3] << 8) | data_buf[2]
            ch_fy = (data_buf[5] << 8) | data_buf[4]
            ch_fxl = (data_buf[7] << 8) | data_buf[6]
            ch_nir = (data_buf[9] << 8) | data_buf[8]
            ch_2x_vis_1 = (data_buf[11] << 8) | data_buf[10]
            ch_f2 = (data_buf[15] << 8) | data_buf[14]
            ch_f3 = (data_buf[17] << 8) | data_buf[16]
            ch_f4 = (data_buf[19] << 8) | data_buf[18]
            ch_f6 = (data_buf[21] << 8) | data_buf[20]
            ch_f1 = (data_buf[27] << 8) | data_buf[26]
            ch_f5 = (data_buf[33] << 8) | data_buf[32]
            ch_f7 = (data_buf[29] << 8) | data_buf[28]
            ch_f8 = (data_buf[31] << 8) | data_buf[30]
            ch_fd_1 = (data_buf[13] << 8) | data_buf[12]

            raw_channels = {
                "f1": ch_f1, "f2": ch_f2, "f3": ch_f3, "f4": ch_f4,
                "f5": ch_f5, "f6": ch_f6, "f7": ch_f7, "f8": ch_f8,
                "fz": ch_fz, "fy": ch_fy, "fxl": ch_fxl, "nir": ch_nir,
                "2x_vis_1": ch_2x_vis_1, "fd_1": ch_fd_1
            }

            scaled_channels = {}
            for ch, val in raw_channels.items():
                offset = self._channel_offsets.get(ch, self._dark_offset)
                val_no_dark = max(0, val - offset)
                scaled = val_no_dark * self._channel_scaling.get(ch, 1.0)
                scaled_channels[ch] = scaled

            self.mark_ready()
            return {
                "sensor": self.name,
                "timestamp": timestamp,
                "status": self.status.value,
                "red": ch_f8,
                "green": ch_fy,
                "blue": ch_fz,
                "lux": scaled_channels["fxl"],
                "raw_fxl": ch_fxl,
                **scaled_channels
            }
        except Exception as exc:
            msg = f"TCS3448 read failed: {exc}"
            self.mark_error(msg)
            LOGGER.exception(msg)
            return {
                "sensor": self.name,
                "timestamp": timestamp,
                "status": self.status.value,
                "error": self.last_error,
            }


class MockTCS3448(BaseSensor):
    """Mock TCS3448 with smooth daylight-like drift."""

    def __init__(
        self,
        seed: int | None = None,
        start_lux: float = 380.0,
        dark_offset: int = 0,
        channel_scaling: dict[str, float] | None = None,
        channel_offsets: dict[str, int] | None = None,
    ) -> None:
        super().__init__(name="tcs3448")
        self._random = random.Random(seed)
        self._lux = start_lux
        self._dark_offset = dark_offset
        self._channel_scaling = channel_scaling or {}
        self._channel_offsets = channel_offsets or {}
        self._r = 120
        self._g = 135
        self._b = 95

    def setup(self) -> bool:
        """Initialize mock state."""
        self.mark_ready()
        LOGGER.info("Mock TCS3448 initialized")
        return True

    def read_data(self) -> dict[str, Any]:
        """Generate one synthetic color/illuminance sample."""
        timestamp = datetime.now(UTC).isoformat()
        try:
            self._lux += self._random.uniform(-20.0, 20.0)
            self._lux = max(0.0, min(3000.0, self._lux))

            self._r = max(0, min(255, self._r + self._random.randint(-6, 6)))
            self._g = max(0, min(255, self._g + self._random.randint(-6, 6)))
            self._b = max(0, min(255, self._b + self._random.randint(-6, 6)))

            raw_channels = {
                "f1": self._random.randint(10, 500),
                "f2": self._random.randint(10, 500),
                "f3": self._random.randint(10, 500),
                "f4": self._random.randint(10, 500),
                "f5": self._random.randint(10, 500),
                "f6": self._random.randint(10, 500),
                "f7": self._random.randint(10, 500),
                "f8": self._random.randint(10, 500),
                "fz": self._random.randint(10, 500),
                "fy": self._random.randint(10, 500),
                "fxl": round(self._lux, 2),
                "nir": self._random.randint(10, 500),
                "2x_vis_1": self._random.randint(10, 500),
                "fd_1": self._random.randint(0, 50),
            }

            scaled_channels = {}
            for ch, val in raw_channels.items():
                offset = self._channel_offsets.get(ch, self._dark_offset)
                val_no_dark = max(0, val - offset)
                scaled = val_no_dark * self._channel_scaling.get(ch, 1.0)
                scaled_channels[ch] = scaled

            self.mark_ready()
            return {
                "sensor": self.name,
                "timestamp": timestamp,
                "status": self.status.value,
                "red": self._r,
                "green": self._g,
                "blue": self._b,
                "lux": scaled_channels["fxl"],
                "raw_fxl": raw_channels["fxl"],
                **scaled_channels
            }
        except Exception as exc:  # noqa: BLE001 - should not happen, but keep mock resilient
            msg = f"Mock TCS3448 read failed unexpectedly: {exc}"
            self.mark_error(msg)
            LOGGER.exception(msg)
            return {
                "sensor": self.name,
                "timestamp": timestamp,
                "status": self.status.value,
                "error": self.last_error,
            }
