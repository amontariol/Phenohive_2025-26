"""Tests for runtime burst sampling and payload aggregation."""

from __future__ import annotations

from datetime import UTC, datetime

from main import _smooth_time_window_records, collect_sensor_data


class SequenceSensor:
    """Return a predefined sequence of payloads on consecutive reads."""

    def __init__(self, payloads: list[dict[str, object]]) -> None:
        self._payloads = payloads
        self._idx = 0

    def read_data(self) -> dict[str, object]:
        payload = self._payloads[min(self._idx, len(self._payloads) - 1)]
        self._idx += 1
        return dict(payload)


def test_collect_sensor_data_aggregates_burst_samples() -> None:
    now = datetime(2026, 3, 29, tzinfo=UTC)
    sensors = {
        "sht35": SequenceSensor(
            [
                {"sensor": "sht35", "timestamp": "t1", "status": "ready", "air_temperature_c": 20.0, "air_humidity_pct": 50.0},
                {"sensor": "sht35", "timestamp": "t2", "status": "ready", "air_temperature_c": 22.0, "air_humidity_pct": 54.0},
                {"sensor": "sht35", "timestamp": "t3", "status": "ready", "air_temperature_c": 21.0, "air_humidity_pct": 52.0},
            ]
        )
    }

    record = collect_sensor_data(
        sensors,
        now_provider=lambda: now,
        station_id="A1",
        sensor_sample_counts={"sht35": 3},
        sample_interval_s=0.0,
        outlier_method="none",
    )

    assert record["timestamp"] == now.isoformat()
    assert record["station_id"] == "A1"
    assert record["sht35_air_temperature_c"] == 21.0
    assert record["sht35_air_humidity_pct"] == 52.0
    assert record["sht35_status"] == "ready"
    assert record["sht35_sample_count"] == 3
    assert record["sht35_successful_samples"] == 3
    assert record["sht35_quality_score"] == 100.0
    assert record["sht35_critical_quality_issue"] is False


def test_collect_sensor_data_uses_single_sample_by_default() -> None:
    now = datetime(2026, 3, 29, tzinfo=UTC)
    sensors = {
        "sht35": SequenceSensor(
            [
                {"sensor": "sht35", "timestamp": "t1", "status": "ready", "air_temperature_c": 21.0},
                {"sensor": "sht35", "timestamp": "t2", "status": "ready", "air_temperature_c": 22.0},
            ]
        )
    }

    record = collect_sensor_data(
        sensors,
        now_provider=lambda: now,
        sensor_sample_counts={},
        sample_interval_s=0.0,
        outlier_method="none",
    )

    assert record["sht35_air_temperature_c"] == 21.0
    assert record["sht35_sample_count"] == 1
    assert record["sht35_successful_samples"] == 1


def test_collect_sensor_data_mad_filter_rejects_spike() -> None:
    now = datetime(2026, 3, 29, tzinfo=UTC)
    sensors = {
        "tcs3448": SequenceSensor(
            [
                {"sensor": "tcs3448", "timestamp": "t1", "status": "ready", "lux": 100.0},
                {"sensor": "tcs3448", "timestamp": "t2", "status": "ready", "lux": 101.0},
                {"sensor": "tcs3448", "timestamp": "t3", "status": "ready", "lux": 1000.0},
                {"sensor": "tcs3448", "timestamp": "t4", "status": "ready", "lux": 102.0},
                {"sensor": "tcs3448", "timestamp": "t5", "status": "ready", "lux": 99.0},
            ]
        )
    }

    record = collect_sensor_data(
        sensors,
        now_provider=lambda: now,
        sensor_sample_counts={"tcs3448": 5},
        sample_interval_s=0.0,
        outlier_method="mad",
        mad_threshold=3.0,
    )

    assert record["tcs3448_lux"] == 100.5


def test_collect_sensor_data_trimmed_filter_rejects_extremes() -> None:
    now = datetime(2026, 3, 29, tzinfo=UTC)
    sensors = {
        "sht35": SequenceSensor(
            [
                {"sensor": "sht35", "timestamp": "t1", "status": "ready", "air_temperature_c": 15.0},
                {"sensor": "sht35", "timestamp": "t2", "status": "ready", "air_temperature_c": 20.0},
                {"sensor": "sht35", "timestamp": "t3", "status": "ready", "air_temperature_c": 21.0},
                {"sensor": "sht35", "timestamp": "t4", "status": "ready", "air_temperature_c": 22.0},
                {"sensor": "sht35", "timestamp": "t5", "status": "ready", "air_temperature_c": 60.0},
            ]
        )
    }

    record = collect_sensor_data(
        sensors,
        now_provider=lambda: now,
        sensor_sample_counts={"sht35": 5},
        sample_interval_s=0.0,
        outlier_method="trimmed",
        trim_ratio=0.2,
    )

    assert record["sht35_air_temperature_c"] == 21.0


def test_collect_sensor_data_applies_plausibility_range_guards() -> None:
    now = datetime(2026, 3, 29, tzinfo=UTC)
    sensors = {
        "tcs3448": SequenceSensor(
            [
                {"sensor": "tcs3448", "timestamp": "t1", "status": "ready", "lux": -8.0},
                {"sensor": "tcs3448", "timestamp": "t2", "status": "ready", "lux": 100.0},
                {"sensor": "tcs3448", "timestamp": "t3", "status": "ready", "lux": 110.0},
            ]
        )
    }

    record = collect_sensor_data(
        sensors,
        now_provider=lambda: now,
        sensor_sample_counts={"tcs3448": 3},
        sample_interval_s=0.0,
        outlier_method="none",
    )

    assert record["tcs3448_lux"] == 105.0
    assert record["tcs3448_invalid_numeric_samples"] == 1


def test_collect_sensor_data_null_policy_when_success_ratio_too_low() -> None:
    now = datetime(2026, 3, 29, tzinfo=UTC)
    sensors = {
        "sht35": SequenceSensor(
            [
                {"sensor": "sht35", "timestamp": "t1", "status": "ready", "air_temperature_c": 21.0},
                {"sensor": "sht35", "timestamp": "t2", "status": "error", "error": "bus timeout"},
            ]
        )
    }

    record = collect_sensor_data(
        sensors,
        now_provider=lambda: now,
        sensor_sample_counts={"sht35": 2},
        sample_interval_s=0.0,
        outlier_method="none",
        min_success_ratio=0.8,
        failed_sample_policy="null",
    )

    assert record["sht35_air_temperature_c"] is None
    assert record["sht35_low_confidence"] is True
    assert record["sht35_failed_policy_applied"] == "null"
    assert record["sht35_critical_quality_issue"] is False


def test_collect_sensor_data_carry_forward_policy_reuses_last_good_value() -> None:
    now = datetime(2026, 3, 29, tzinfo=UTC)
    sensors = {
        "sht35": SequenceSensor(
            [
                {"sensor": "sht35", "timestamp": "t1", "status": "ready", "air_temperature_c": 21.0},
                {"sensor": "sht35", "timestamp": "t2", "status": "error", "error": "crc"},
            ]
        )
    }
    last_good_payloads = {
        "sht35": {
            "air_temperature_c": 20.0,
        }
    }

    record = collect_sensor_data(
        sensors,
        now_provider=lambda: now,
        sensor_sample_counts={"sht35": 2},
        sample_interval_s=0.0,
        outlier_method="none",
        min_success_ratio=0.8,
        failed_sample_policy="carry_forward",
        last_good_payloads=last_good_payloads,
    )

    assert record["sht35_air_temperature_c"] == 20.0
    assert record["sht35_failed_policy_applied"] == "carry_forward"
    assert record["sht35_low_confidence"] is True


def test_collect_sensor_data_calibration_drift_and_due_check() -> None:
    calibration_state: dict[str, dict[str, object]] = {}
    last_good_payloads: dict[str, dict[str, object]] = {}
    sensors = {
        "scale_hx711": SequenceSensor(
            [
                {"sensor": "scale_hx711", "timestamp": "t1", "status": "ready", "weight_g": 100.0},
            ]
        )
    }

    first_time = datetime(2026, 3, 29, 0, 0, tzinfo=UTC)
    first_record = collect_sensor_data(
        sensors,
        now_provider=lambda: first_time,
        sensor_sample_counts={"scale_hx711": 1},
        outlier_method="none",
        calibration_enabled=True,
        calibration_interval_h=1.0,
        calibration_state=calibration_state,
        calibration_drift_warn_pct={"scale_hx711": 5.0},
        last_good_payloads=last_good_payloads,
    )

    assert first_record["scale_hx711_drift_pct"] == 0.0
    assert first_record["scale_hx711_calibration_due"] is False

    sensors["scale_hx711"] = SequenceSensor(
        [
            {"sensor": "scale_hx711", "timestamp": "t2", "status": "ready", "weight_g": 120.0},
        ]
    )
    second_time = datetime(2026, 3, 29, 0, 30, tzinfo=UTC)
    second_record = collect_sensor_data(
        sensors,
        now_provider=lambda: second_time,
        sensor_sample_counts={"scale_hx711": 1},
        outlier_method="none",
        calibration_enabled=True,
        calibration_interval_h=1.0,
        calibration_state=calibration_state,
        calibration_drift_warn_pct={"scale_hx711": 5.0},
        last_good_payloads=last_good_payloads,
    )

    assert second_record["scale_hx711_drift_alert"] is True
    assert second_record["scale_hx711_calibration_due"] is False
    assert second_record["scale_hx711_critical_quality_issue"] is True


def test_collect_sensor_data_marks_critical_issue_for_many_invalid_samples() -> None:
    now = datetime(2026, 3, 29, tzinfo=UTC)
    sensors = {
        "tcs3448": SequenceSensor(
            [
                {"sensor": "tcs3448", "timestamp": "t1", "status": "ready", "lux": -10.0},
                {"sensor": "tcs3448", "timestamp": "t2", "status": "ready", "lux": -8.0},
                {"sensor": "tcs3448", "timestamp": "t3", "status": "ready", "lux": 100.0},
                {"sensor": "tcs3448", "timestamp": "t4", "status": "ready", "lux": 105.0},
            ]
        )
    }

    record = collect_sensor_data(
        sensors,
        now_provider=lambda: now,
        sensor_sample_counts={"tcs3448": 4},
        sample_interval_s=0.0,
        outlier_method="none",
    )

    assert record["tcs3448_invalid_numeric_samples"] == 2
    assert record["tcs3448_critical_quality_issue"] is True
    assert record["tcs3448_quality_score"] < 100.0


def test_collect_sensor_data_scale_policy_override_prevents_stale_weight() -> None:
    now = datetime(2026, 3, 29, tzinfo=UTC)
    sensors = {
        "scale_hx711": SequenceSensor(
            [
                {"sensor": "scale_hx711", "timestamp": "t1", "status": "error", "error": "read failed"},
                {"sensor": "scale_hx711", "timestamp": "t2", "status": "error", "error": "read failed"},
            ]
        )
    }

    record = collect_sensor_data(
        sensors,
        now_provider=lambda: now,
        sensor_sample_counts={"scale_hx711": 2},
        sample_interval_s=0.0,
        outlier_method="none",
        min_success_ratio=0.8,
        failed_sample_policy="mark_only",
        sensor_failed_sample_policies={"scale_hx711": "carry_forward"},
        last_good_payloads={"scale_hx711": {"weight_g": 345.6}},
    )

    assert record["scale_hx711_weight_g"] == 345.6
    assert record["scale_hx711_failed_policy_applied"] == "carry_forward"


def test_smooth_time_window_records_averages_measurements_over_time() -> None:
    now = datetime(2026, 4, 5, 10, 10, tzinfo=UTC)
    records = [
        {
            "timestamp": "2026-04-05T10:00:00+00:00",
            "station_id": "01",
            "sht35_air_temperature_c": 20.0,
            "sht35_air_humidity_pct": 50.0,
        },
        {
            "timestamp": "2026-04-05T10:02:00+00:00",
            "station_id": "01",
            "sht35_air_temperature_c": 24.0,
            "sht35_air_humidity_pct": 54.0,
        },
        {
            "timestamp": "2026-04-05T10:04:00+00:00",
            "station_id": "01",
            "sht35_air_temperature_c": 22.0,
            "sht35_air_humidity_pct": 52.0,
        },
    ]

    smoothed = _smooth_time_window_records(
        records=records,
        now_provider=lambda: now,
        outlier_method="none",
        mad_threshold=3.0,
        trim_ratio=0.2,
    )

    assert smoothed["timestamp"] == now.isoformat()
    assert smoothed["station_id"] == "01"
    assert smoothed["sht35_air_temperature_c"] == 22.0
    assert smoothed["sht35_air_temperature_c_median"] == 22.0
    assert smoothed["sht35_air_temperature_c_min"] == 20.0
    assert smoothed["sht35_air_temperature_c_max"] == 24.0
    assert smoothed["sht35_air_temperature_c_stddev"] == 2.0
    assert smoothed["sht35_air_humidity_pct"] == 52.0
    assert smoothed["sht35_air_humidity_pct_median"] == 52.0
    assert smoothed["sht35_air_humidity_pct_min"] == 50.0
    assert smoothed["sht35_air_humidity_pct_max"] == 54.0
    assert smoothed["sht35_air_humidity_pct_stddev"] == 2.0
    assert smoothed["sampling_window_records"] == 3
    assert smoothed["sampling_window_start_utc"] == "2026-04-05T10:00:00+00:00"
    assert smoothed["sampling_window_end_utc"] == "2026-04-05T10:04:00+00:00"


def test_smooth_time_window_records_keeps_latest_quality_metadata() -> None:
    now = datetime(2026, 4, 5, 10, 10, tzinfo=UTC)
    records = [
        {
            "timestamp": "2026-04-05T10:00:00+00:00",
            "scale_hx711_weight_g": 100.0,
            "scale_hx711_quality_score": 95.0,
            "scale_hx711_low_confidence": False,
            "scale_hx711_failed_policy_applied": "mark_only",
        },
        {
            "timestamp": "2026-04-05T10:02:00+00:00",
            "scale_hx711_weight_g": 102.0,
            "scale_hx711_quality_score": 70.0,
            "scale_hx711_low_confidence": True,
            "scale_hx711_failed_policy_applied": "carry_forward",
        },
    ]

    smoothed = _smooth_time_window_records(
        records=records,
        now_provider=lambda: now,
        outlier_method="none",
        mad_threshold=3.0,
        trim_ratio=0.2,
    )

    assert smoothed["scale_hx711_weight_g"] == 101.0
    assert smoothed["scale_hx711_weight_g_median"] == 101.0
    assert smoothed["scale_hx711_weight_g_min"] == 100.0
    assert smoothed["scale_hx711_weight_g_max"] == 102.0
    assert smoothed["scale_hx711_weight_g_stddev"] == 1.4142
    assert smoothed["scale_hx711_quality_score"] == 70.0
    assert smoothed["scale_hx711_low_confidence"] is True
    assert smoothed["scale_hx711_failed_policy_applied"] == "carry_forward"
