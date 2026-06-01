"""Tests for real/mock wiring in the sensor factory."""

from __future__ import annotations

from pathlib import Path

from src.core.config_manager import ConfigManager
from src.core.sensor_factory import SensorFactory
from src.sensors import (
    MockScaleHX711,
    MockSHT35,
    MockTCS3448,
    RealScaleHX711,
    RealSHT35,
    RealTCS3448,
)


def write_ini(path: Path, content: str) -> None:
    """Write INI text to disk."""
    path.write_text(content.strip() + "\n", encoding="utf-8")


def test_factory_creates_mock_sensors_when_enabled(tmp_path: Path) -> None:
    """Mock mode should create only mock sensor instances."""
    ini_path = tmp_path / "config.ini"
    write_ini(
        ini_path,
        """
        [general]
        mock_mode = true

        [sensors]
        sht35 = mock
        tcs3448 = mock
        scale_hx711 = mock
        """,
    )

    cfg = ConfigManager(ini_path)
    cfg.load()

    factory = SensorFactory(cfg)
    sensors = factory.create_all()

    assert isinstance(sensors["sht35"], MockSHT35)
    assert isinstance(sensors["tcs3448"], MockTCS3448)
    assert isinstance(sensors["scale_hx711"], MockScaleHX711)


def test_factory_creates_real_sensors_with_hex_addresses(tmp_path: Path) -> None:
    """Hex addresses should be parsed for real sensor creation."""
    ini_path = tmp_path / "config.ini"
    write_ini(
        ini_path,
        """
        [general]
        mock_mode = false

        [sensors]
        sht35 = real
        tcs3448 = real
        scale_hx711 = real

        [sht35]
        i2c_address = 0x44

        [tcs3448]
        i2c_address = 0x29
        integration_time = 60
        gain = 16

        [scale_hx711]
        dout_pin = 17
        pd_sck_pin = 27
        samples = 5
        tare = 10.0
        calibration_factor = 2.0
        """,
    )

    cfg = ConfigManager(ini_path)
    cfg.load()

    factory = SensorFactory(cfg)
    sensors = factory.create_all()

    assert isinstance(sensors["sht35"], RealSHT35)
    assert isinstance(sensors["tcs3448"], RealTCS3448)
    assert isinstance(sensors["scale_hx711"], RealScaleHX711)


def test_factory_respects_per_sensor_mock_override(tmp_path: Path) -> None:
    """Per-sensor [sensors] entries should override global mock_mode=false."""
    ini_path = tmp_path / "config.ini"
    write_ini(
        ini_path,
        """
        [general]
        mock_mode = false

        [sensors]
        sht35 = mock
        tcs3448 = mock
        scale_hx711 = real
        """,
    )

    cfg = ConfigManager(ini_path)
    cfg.load()

    factory = SensorFactory(cfg)
    sensors = factory.create_all()

    assert isinstance(sensors["sht35"], MockSHT35)
    assert isinstance(sensors["tcs3448"], MockTCS3448)
    assert isinstance(sensors["scale_hx711"], RealScaleHX711)


def test_setup_all_runs_setup_for_each_sensor(tmp_path: Path) -> None:
    """setup_all should call setup and return per-sensor results."""
    ini_path = tmp_path / "config.ini"
    write_ini(
        ini_path,
        """
        [general]
        mock_mode = true
        """,
    )

    cfg = ConfigManager(ini_path)
    cfg.load()

    factory = SensorFactory(cfg)
    sensors = factory.create_all()
    results = factory.setup_all(sensors)

    assert results == {
        "sht35": True,
        "tcs3448": True,
        "scale_hx711": True,
    }
