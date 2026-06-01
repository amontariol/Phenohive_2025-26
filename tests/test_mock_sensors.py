"""Contract and range checks for mock sensors."""

from __future__ import annotations

from src.sensors import MockScaleHX711, MockSHT35, MockTCS3448

_TCS_CHANNELS = ("f1", "f2", "f3", "f4", "f5", "f6", "f7", "f8", "fz", "fy", "fxl", "nir", "2x_vis_1", "fd_1")


def test_mock_sht35_contract_and_ranges() -> None:
    sensor = MockSHT35(seed=123)
    assert sensor.setup() is True

    payload = sensor.read_data()

    assert payload["sensor"] == "sht35"
    assert payload["status"] == "ready"
    assert 10.0 <= payload["air_temperature_c"] <= 40.0
    assert 15.0 <= payload["air_humidity_pct"] <= 95.0


def test_mock_sht35_returns_raw_fields() -> None:
    """Mock SHT35 should also expose raw (pre-offset) temperature and humidity."""
    sensor = MockSHT35(seed=42)
    sensor.setup()
    payload = sensor.read_data()

    assert "raw_temperature_c" in payload
    assert "raw_humidity_pct" in payload
    assert isinstance(payload["raw_temperature_c"], float)
    assert isinstance(payload["raw_humidity_pct"], float)


def test_mock_sht35_offset_applied() -> None:
    """Calibration offsets should shift the reported values relative to raw."""
    sensor = MockSHT35(seed=7, temp_offset=2.0, hum_offset=-5.0)
    sensor.setup()
    payload = sensor.read_data()

    assert abs(payload["air_temperature_c"] - (payload["raw_temperature_c"] + 2.0)) < 0.01
    assert abs(payload["air_humidity_pct"] - (payload["raw_humidity_pct"] - 5.0)) < 0.01


def test_mock_tcs3448_contract_and_ranges() -> None:
    sensor = MockTCS3448(seed=123)
    assert sensor.setup() is True

    payload = sensor.read_data()

    assert payload["sensor"] == "tcs3448"
    assert payload["status"] == "ready"
    assert 0.0 <= payload["lux"] <= 3000.0
    assert 0 <= payload["red"] <= 255
    assert 0 <= payload["green"] <= 255
    assert 0 <= payload["blue"] <= 255


def test_mock_tcs3448_returns_all_spectral_channels() -> None:
    """All 14 spectral channels and the raw_fxl convenience field must be present."""
    sensor = MockTCS3448(seed=99)
    sensor.setup()
    payload = sensor.read_data()

    for ch in _TCS_CHANNELS:
        assert ch in payload, f"Missing spectral channel: {ch}"
    assert "raw_fxl" in payload


def test_mock_tcs3448_channel_values_are_non_negative() -> None:
    """After dark-offset subtraction, all scaled channel values should be >= 0."""
    sensor = MockTCS3448(seed=55, dark_offset=10)
    sensor.setup()
    payload = sensor.read_data()

    for ch in _TCS_CHANNELS:
        assert payload[ch] >= 0, f"Channel {ch} went negative: {payload[ch]}"


def test_mock_hx711_contract_and_ranges() -> None:
    sensor = MockScaleHX711(seed=123)
    assert sensor.setup() is True

    payload = sensor.read_data()

    assert payload["sensor"] == "scale_hx711"
    assert payload["status"] == "ready"
    assert payload["sample_count"] == 10
    assert 100.0 <= payload["weight_g"] <= 1200.0
    assert payload["weight_stddev_raw"] >= 0.0


def test_mock_hx711_returns_weight_raw() -> None:
    """Mock HX711 should expose the simulated raw ADC value."""
    sensor = MockScaleHX711(seed=1)
    sensor.setup()
    payload = sensor.read_data()

    assert "weight_raw" in payload
    assert isinstance(payload["weight_raw"], float)


def test_mock_hx711_update_setting_changes_tare() -> None:
    """update_setting('tare', ...) should shift the reported weight accordingly."""
    sensor = MockScaleHX711(seed=5, start_weight_g=500.0)
    sensor.setup()

    before = sensor.read_data()["weight_g"]
    sensor.update_setting("tare", 1000.0)
    # After a large tare increase the reported weight should decrease.
    after = sensor.read_data()["weight_g"]

    assert after < before
