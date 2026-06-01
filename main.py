"""Main runtime loop for PhenoHive."""

from __future__ import annotations

import argparse
import logging
import time
import threading
import numbers
import statistics
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

import os

from src.core.config_manager import load_config
from src.core.data_manager import DataManager
from src.core.logger import setup_logger
from src.core.sensor_factory import SensorFactory
from src.sensors import SensorStatus
from src.core.time_sync import build_time_sync_from_config
from src.sensors.led_strip import LedStripService, MockLedStripService
from src.ui.debug_server import DebugUIService
from src.vision import CameraService, PlantImageProcessor, VisionConfig

PLAUSIBLE_RANGES: dict[str, dict[str, tuple[float, float]]] = {
    "sht35": {
        "air_temperature_c": (-40.0, 85.0),
        "air_humidity_pct": (0.0, 100.0),
        "raw_temperature_c": (-40.0, 85.0),
        "raw_humidity_pct": (0.0, 100.0),
    },

    "tcs3448": {
        "red": (0.0, 65535.0),
        "green": (0.0, 65535.0),
        "blue": (0.0, 65535.0),
        "lux": (0.0, 1000000.0),
        "raw_fxl": (0.0, 65535.0),
        "f1": (0.0, 65535.0), "f2": (0.0, 65535.0), "f3": (0.0, 65535.0), "f4": (0.0, 65535.0),
        "f5": (0.0, 65535.0), "f6": (0.0, 65535.0), "f7": (0.0, 65535.0), "f8": (0.0, 65535.0),
        "fz": (0.0, 65535.0), "fy": (0.0, 65535.0), "fxl": (0.0, 65535.0), "nir": (0.0, 65535.0),
        "2x_vis_1": (0.0, 65535.0), "fd_1": (0.0, 65535.0)
    },
    "scale_hx711": {
        # weight_raw is a raw ADC count (can be millions) — no plausibility range
        "weight_g": (-1_000_000.0, 1_000_000.0),
        "weight_stddev_raw": (0.0, 500_000_000.0),
    },
}

CALIBRATION_REFERENCE_FIELDS: dict[str, str] = {
    "scale_hx711": "weight_g",
}

SENSOR_METADATA_SUFFIXES: tuple[str, ...] = (
    "sample_count",
    "successful_samples",
    "success_ratio",
    "invalid_numeric_samples",
    "max_cv",
    "low_confidence",
    "quality_score",
    "critical_quality_issue",
    "failed_policy_applied",
    "calibration_due",
    "drift_alert",
    "drift_pct",
    "last_calibration_utc",
    "calibration_rechecked",
)


def parse_args() -> argparse.Namespace:
    """Parse CLI flags."""
    parser = argparse.ArgumentParser(description="PhenoHive runtime")
    parser.add_argument("--config", default="config.ini", help="Path to configuration file")
    parser.add_argument("--once", action="store_true", help="Run only one measurement cycle")
    parser.add_argument("--log-level", default="INFO", help="Logging level")
    return parser.parse_args()


def build_vision(config: Any) -> tuple[CameraService | None, PlantImageProcessor | None]:
    """Create camera and processor services when vision is enabled."""
    camera_enabled = config.get_bool("camera", "enabled", fallback=True)
    if not camera_enabled:
        return None, None

    camera_service = CameraService()
    if not camera_service.setup():
        return None, None

    vision_config = VisionConfig(
        channel=config.get_str("vision", "channel", fallback="k") or "k",
        kernel_size=config.get_int("vision", "kernel_size", fallback=20),
        sigma=config.get_float("vision", "sigma", fallback=2.0),
        skeleton_output=config.get_str("vision", "skeleton_output", fallback="data/images/skeleton.jpg")
        or "data/images/skeleton.jpg",
        background_image=config.get_str("vision", "background_image", fallback="data/images/background.jpg")
        or "data/images/background.jpg",
        pixel_to_cm_ratio=config.get_float("vision", "pixel_to_cm_ratio", fallback=1.0),
    )
    image_processor = PlantImageProcessor(config=vision_config)
    return camera_service, image_processor


def collect_sensor_data(
    sensors: dict[str, Any],
    now_provider: Callable[[], datetime],
    station_id: str | int | None = None,
    sensor_sample_counts: dict[str, int] | None = None,
    sample_interval_s: float = 0.0,
    outlier_method: str = "mad",
    mad_threshold: float = 3.0,
    trim_ratio: float = 0.2,
    min_success_ratio: float = 0.6,
    failed_sample_policy: str = "mark_only",
    hardware_uuid: str | None = None,
    sensor_failed_sample_policies: dict[str, str] | None = None,
    max_cv_for_confidence: float = 0.35,
    last_good_payloads: dict[str, dict[str, Any]] | None = None,
    plausibility_ranges: dict[str, dict[str, tuple[float, float]]] | None = None,
    calibration_enabled: bool = False,
    calibration_interval_h: float = 168.0,
    calibration_state: dict[str, dict[str, Any]] | None = None,
    calibration_reference_fields: dict[str, str] | None = None,
    calibration_drift_warn_pct: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Read all sensors with optional burst sampling and flatten into one record."""
    cycle_time = now_provider()
    record: dict[str, Any] = {
        "timestamp": cycle_time.isoformat(),
    }

    if station_id is not None:
        record["station_id"] = station_id
    if hardware_uuid is not None:
        record["hardware_uuid"] = hardware_uuid

    sample_counts = sensor_sample_counts or {}
    spacing = max(0.0, sample_interval_s)
    min_ratio = min(max(min_success_ratio, 0.0), 1.0)
    normalized_failed_policy = failed_sample_policy.strip().lower()
    per_sensor_policy = sensor_failed_sample_policies or {}
    previous_good = last_good_payloads if last_good_payloads is not None else {}
    ranges = plausibility_ranges or PLAUSIBLE_RANGES
    calibration_snapshot = calibration_state if calibration_state is not None else {}
    reference_fields = calibration_reference_fields or CALIBRATION_REFERENCE_FIELDS
    drift_warn_pct = calibration_drift_warn_pct or {}

    for sensor_key, sensor in sensors.items():
        sample_count = max(1, int(sample_counts.get(sensor_key, 1)))
        payloads: list[dict[str, Any]] = []

        for idx in range(sample_count):
            payloads.append(sensor.read_data())
            if idx < sample_count - 1 and spacing > 0.0:
                time.sleep(spacing)

        payload = _aggregate_sensor_payloads(
            payloads,
            sensor_key=sensor_key,
            outlier_method=outlier_method,
            mad_threshold=mad_threshold,
            trim_ratio=trim_ratio,
            plausibility_range_by_field=ranges.get(sensor_key, {}),
            min_success_ratio=min_ratio,
            failed_sample_policy=(
                per_sensor_policy.get(sensor_key, normalized_failed_policy).strip().lower()
            ),
            previous_good_payload=previous_good.get(sensor_key),
            max_cv_for_confidence=max_cv_for_confidence,
        )
        if payload.get("success_ratio", 0.0) >= min_ratio:
            previous_good[sensor_key] = dict(payload)

        if calibration_enabled:
            calibration_payload = _apply_calibration_check(
                sensor_key=sensor_key,
                payload=payload,
                cycle_time=cycle_time,
                calibration_interval_h=max(1.0, calibration_interval_h),
                calibration_state=calibration_snapshot,
                reference_field=reference_fields.get(sensor_key),
                drift_warn_pct=drift_warn_pct.get(sensor_key, 10.0),
                min_success_ratio=min_ratio,
            )
            payload.update(calibration_payload)
            if payload.get("drift_alert", False):
                payload["critical_quality_issue"] = True

        for field, value in payload.items():
            if field in {"timestamp", "sensor"}:
                continue
            record[f"{sensor_key}_{field}"] = value

    return record


def _aggregate_sensor_payloads(
    payloads: list[dict[str, Any]],
    sensor_key: str,
    outlier_method: str,
    mad_threshold: float,
    trim_ratio: float,
    plausibility_range_by_field: dict[str, tuple[float, float]],
    min_success_ratio: float,
    failed_sample_policy: str,
    previous_good_payload: dict[str, Any] | None,
    max_cv_for_confidence: float,
) -> dict[str, Any]:
    """Merge one sensor's burst payloads into a single representative payload."""
    if not payloads:
        return {}

    aggregated: dict[str, Any] = {}
    fields: set[str] = set()
    for payload in payloads:
        fields.update(payload.keys())

    max_cv: float = 0.0
    invalid_numeric_samples = 0
    for field in fields:
        values = [payload.get(field) for payload in payloads]
        non_null_values = [value for value in values if value is not None]
        if not non_null_values:
            continue

        if all(isinstance(value, numbers.Real) and not isinstance(value, bool) for value in non_null_values):
            numeric_values = [float(value) for value in non_null_values]
            if field in plausibility_range_by_field:
                min_allowed, max_allowed = plausibility_range_by_field[field]
                valid_values = [value for value in numeric_values if min_allowed <= value <= max_allowed]
                invalid_numeric_samples += len(numeric_values) - len(valid_values)
                numeric_values = valid_values
            if not numeric_values:
                continue

            filtered_values = _filter_numeric_values(
                numeric_values,
                method=outlier_method,
                mad_threshold=mad_threshold,
                trim_ratio=trim_ratio,
            )
            mean_value = sum(filtered_values) / len(filtered_values)
            aggregated[field] = round(mean_value, 4)
            if len(filtered_values) > 1 and mean_value != 0:
                stddev = statistics.stdev(filtered_values)
                max_cv = max(max_cv, abs(stddev / mean_value))
        else:
            aggregated[field] = non_null_values[-1]

    aggregated["sample_count"] = len(payloads)
    successful_samples = sum(1 for payload in payloads if payload.get("status") == "ready")
    success_ratio = successful_samples / len(payloads)
    low_confidence = success_ratio < min_success_ratio or max_cv > max_cv_for_confidence
    quality_score = _calculate_quality_score(
        success_ratio=success_ratio,
        max_cv=max_cv,
        invalid_numeric_samples=invalid_numeric_samples,
        sample_count=len(payloads),
        max_cv_for_confidence=max_cv_for_confidence,
    )
    critical_quality_issue = (
        success_ratio < max(0.2, min_success_ratio * 0.5)
        or invalid_numeric_samples >= max(1, len(payloads) // 2)
    )

    aggregated["successful_samples"] = successful_samples
    aggregated["success_ratio"] = round(success_ratio, 4)
    aggregated["invalid_numeric_samples"] = invalid_numeric_samples
    aggregated["max_cv"] = round(max_cv, 4)
    aggregated["low_confidence"] = low_confidence
    aggregated["quality_score"] = quality_score
    aggregated["critical_quality_issue"] = critical_quality_issue

    if success_ratio < min_success_ratio:
        protected_fields = {
            "sample_count",
            "successful_samples",
            "success_ratio",
            "invalid_numeric_samples",
            "max_cv",
            "low_confidence",
        }
        numeric_fields = {
            field
            for field, value in aggregated.items()
            if isinstance(value, numbers.Real) and not isinstance(value, bool) and field not in protected_fields
        }
        if previous_good_payload is not None:
            numeric_fields.update(
                field
                for field, value in previous_good_payload.items()
                if isinstance(value, numbers.Real) and not isinstance(value, bool) and field not in protected_fields
            )
        if failed_sample_policy == "null":
            for field in numeric_fields:
                aggregated[field] = None
            aggregated["failed_policy_applied"] = "null"
        elif failed_sample_policy == "carry_forward":
            for field in numeric_fields:
                replacement = None
                if previous_good_payload is not None:
                    replacement = previous_good_payload.get(field)
                aggregated[field] = replacement
            aggregated["failed_policy_applied"] = "carry_forward"
        else:
            aggregated["failed_policy_applied"] = "mark_only"

    return aggregated


def _apply_calibration_check(
    sensor_key: str,
    payload: dict[str, Any],
    cycle_time: datetime,
    calibration_interval_h: float,
    calibration_state: dict[str, dict[str, Any]],
    reference_field: str | None,
    drift_warn_pct: float,
    min_success_ratio: float,
) -> dict[str, Any]:
    """Track baseline drift and periodic calibration windows for critical sensors."""
    if reference_field is None:
        return {
            "calibration_due": False,
            "drift_alert": False,
            "drift_pct": 0.0,
            "last_calibration_utc": None,
            "calibration_rechecked": False,
        }

    value = payload.get(reference_field)
    if not isinstance(value, numbers.Real) or isinstance(value, bool):
        return {
            "calibration_due": False,
            "drift_alert": False,
            "drift_pct": 0.0,
            "last_calibration_utc": None,
            "calibration_rechecked": False,
        }

    sensor_state = calibration_state.get(sensor_key)
    if sensor_state is None:
        sensor_state = {
            "baseline": float(value),
            "last_calibration": cycle_time,
        }
        calibration_state[sensor_key] = sensor_state

    baseline = float(sensor_state.get("baseline", value))
    last_calibration = sensor_state.get("last_calibration", cycle_time)
    if isinstance(last_calibration, str):
        try:
            last_calibration = datetime.fromisoformat(last_calibration)
        except ValueError:
            last_calibration = cycle_time

    elapsed_hours = max(0.0, (cycle_time - last_calibration).total_seconds() / 3600.0)
    calibration_due = elapsed_hours >= calibration_interval_h

    if baseline == 0:
        drift_pct = 0.0 if float(value) == 0.0 else 100.0
    else:
        drift_pct = abs((float(value) - baseline) / baseline) * 100.0
    drift_alert = drift_pct >= max(0.1, drift_warn_pct)

    calibration_rechecked = False
    if calibration_due and payload.get("success_ratio", 0.0) >= min_success_ratio and not payload.get("low_confidence", False):
        sensor_state["baseline"] = float(value)
        sensor_state["last_calibration"] = cycle_time
        baseline = float(value)
        drift_pct = 0.0
        drift_alert = False
        calibration_due = False
        calibration_rechecked = True
    else:
        sensor_state["baseline"] = baseline
        sensor_state["last_calibration"] = last_calibration

    return {
        "calibration_due": calibration_due,
        "drift_alert": drift_alert,
        "drift_pct": round(drift_pct, 4),
        "last_calibration_utc": sensor_state["last_calibration"].isoformat(),
        "calibration_rechecked": calibration_rechecked,
    }


def _calculate_quality_score(
    success_ratio: float,
    max_cv: float,
    invalid_numeric_samples: int,
    sample_count: int,
    max_cv_for_confidence: float,
) -> float:
    """Compute a 0-100 quality score combining success ratio and stability."""
    sample_count = max(1, sample_count)
    normalized_success = min(max(success_ratio, 0.0), 1.0)
    invalid_ratio = min(max(invalid_numeric_samples / sample_count, 0.0), 1.0)

    cv_excess = 0.0
    if max_cv_for_confidence > 0:
        cv_excess = max(0.0, max_cv - max_cv_for_confidence) / max_cv_for_confidence
    cv_penalty = min(1.0, cv_excess)

    # Weighted score: success ratio is primary, then implausible-value and variance penalties.
    score = (normalized_success * 100.0) - (invalid_ratio * 25.0) - (cv_penalty * 15.0)
    return round(min(max(score, 0.0), 100.0), 2)


def _filter_numeric_values(
    values: list[float],
    method: str,
    mad_threshold: float,
    trim_ratio: float,
) -> list[float]:
    """Apply configurable outlier filtering to a numeric burst."""
    if len(values) <= 2:
        return values

    normalized_method = method.strip().lower()
    if normalized_method == "none":
        return values

    if normalized_method == "trimmed":
        ratio = min(max(trim_ratio, 0.0), 0.49)
        trim_n = int(len(values) * ratio)
        if trim_n <= 0 or len(values) - (2 * trim_n) < 1:
            return values
        sorted_values = sorted(values)
        return sorted_values[trim_n : len(sorted_values) - trim_n]

    # Default to MAD-based filtering for robustness against spikes.
    median_value = statistics.median(values)
    deviations = [abs(value - median_value) for value in values]
    mad = statistics.median(deviations)
    if mad == 0:
        return values

    threshold = max(0.1, mad_threshold)
    filtered = [value for value in values if abs(value - median_value) <= threshold * mad]
    return filtered or values


def _is_sensor_metadata_key(field_key: str) -> bool:
    """Identify per-sensor metadata keys that should not be averaged over time."""
    return any(field_key.endswith(f"_{suffix}") for suffix in SENSOR_METADATA_SUFFIXES)


def _smooth_time_window_records(
    records: list[dict[str, Any]],
    now_provider: Callable[[], datetime],
    outlier_method: str,
    mad_threshold: float,
    trim_ratio: float,
) -> dict[str, Any]:
    """Aggregate multiple collection records into one time-window-smoothed publish record."""
    publish_time = now_provider()
    smoothed: dict[str, Any] = {
        "timestamp": publish_time.isoformat(),
        "sampling_window_records": len(records),
    }
    if not records:
        return smoothed

    first_timestamp = records[0].get("timestamp")
    last_timestamp = records[-1].get("timestamp")
    smoothed["sampling_window_start_utc"] = first_timestamp
    smoothed["sampling_window_end_utc"] = last_timestamp

    station_id = records[-1].get("station_id")
    if station_id is not None:
        smoothed["station_id"] = station_id

    fields: set[str] = set()
    for record in records:
        fields.update(record.keys())

    for field in fields:
        if field in {"timestamp", "station_id"}:
            continue

        non_null_values = [record.get(field) for record in records if record.get(field) is not None]
        if not non_null_values:
            continue

        if _is_sensor_metadata_key(field):
            smoothed[field] = non_null_values[-1]
            continue

        if all(isinstance(value, numbers.Real) and not isinstance(value, bool) for value in non_null_values):
            numeric_values = [float(value) for value in non_null_values]
            filtered_values = _filter_numeric_values(
                numeric_values,
                method=outlier_method,
                mad_threshold=mad_threshold,
                trim_ratio=trim_ratio,
            )
            window_mean = sum(filtered_values) / len(filtered_values)
            smoothed[field] = round(window_mean, 4)
            smoothed[f"{field}_median"] = round(statistics.median(filtered_values), 4)
            smoothed[f"{field}_min"] = round(min(filtered_values), 4)
            smoothed[f"{field}_max"] = round(max(filtered_values), 4)
            smoothed[f"{field}_stddev"] = round(statistics.stdev(filtered_values), 4) if len(filtered_values) > 1 else 0.0
            continue

        smoothed[field] = non_null_values[-1]

    return smoothed


def main() -> None:
    """Run the periodic measurement loop."""
    args = parse_args()
    config = load_config(args.config)

    log_dir = config.get_str("paths", "log_dir", fallback="logs") or "logs"
    logger = setup_logger(log_dir=log_dir, level=args.log_level, logger_name="phenohive")
    logger.info("Starting PhenoHive runtime")

    mock_mode = config.is_mock_mode()
    influx_enabled = config.get_bool("influxdb", "enabled", fallback=False)
    influx_url = config.get_str("influxdb", "url", fallback="http://localhost:8086") or "http://localhost:8086"
    logger.info("Runtime config: mock_mode=%s influx_enabled=%s influx_url=%s", mock_mode, influx_enabled, influx_url)

    factory = SensorFactory(config)
    sensors = factory.create_all()

    # Load identity early so it's available to the Debug UI service
    hardware_uuid = config.ensure_uuid()
    station_id = config.get_str("general", "station_id", fallback="1") or "1"

    # Pre-declare variables for the debug UI closure
    setup_results: dict[str, bool] = {}
    time_sync = None
    camera_service = None
    image_processor = None
    collection_interval_s = 0
    publish_interval_s = 0
    latest_cycle: dict[str, Any] = {
        "last_cycle_utc": None,
        "last_cycle_ok": None,
        "last_error": None,
    }
    recent_captures: list[dict[str, Any]] = []
    latest_raw_sample: dict[str, Any] = {}
    latest_vision_capture: dict[str, Any] = {}
    image_output_dir = config.get_str("camera", "image_output_dir", fallback="data/images") or "data/images"

    debug_ui_service: DebugUIService | None = None
    if config.get_bool("debug_ui", "enabled", fallback=False):
        debug_host = config.get_str("debug_ui", "host", fallback="0.0.0.0") or "0.0.0.0"
        debug_port = config.get_int("debug_ui", "port", fallback=8080)
        debug_write_token = config.get_str("debug_ui", "write_token", fallback="") or ""
        debug_ta_password = config.get_str("debug_ui", "ta_password", fallback="") or ""
        debug_allow_remote_writes = config.get_bool("debug_ui", "allow_remote_writes", fallback=False)

        def _runtime_status() -> dict[str, Any]:
            sensors_status = {
                key: {
                    "name": sensor.name,
                    "status": sensor.status.value,
                    "last_error": sensor.last_error,
                }
                for key, sensor in sensors.items()
            }
            if camera_service is not None:
                sensors_status["camera"] = {
                    "name": "Pi Camera",
                    "status": "ready" if camera_service.is_ready else "error",
                    "last_error": None if camera_service.is_ready else "Setup failed",
                }
            else:
                sensors_status["camera"] = {
                    "name": "Pi Camera",
                    "status": "offline",
                    "last_error": "Disabled in config",
                }

            return {
                "now_utc": datetime.now(UTC).isoformat(),
                "config_path": str(args.config),
                "env_overrides": config.get_all_env_overrides(),
                "collection_interval_s": collection_interval_s,
                "publish_interval_s": publish_interval_s,
                "station_id": station_id,
                "hardware_uuid": hardware_uuid,
                "setup_results": setup_results,
                "latest_cycle": latest_cycle,
                "recent_captures": list(recent_captures),
                "latest_raw_sample": dict(latest_raw_sample),
                "latest_vision_capture": dict(latest_vision_capture),
                "time_sync": time_sync.diagnostics() if time_sync else {},
                "sensors": sensors_status,
            }

        def _validate_config_change(section, option, value):
            logger.info("VALIDATOR: Executing for %s.%s = %s", section, option, value)
            target_id = str(value).strip().strip('"').strip("'")
            
            # We only care about station_id changes
            if section != "general" or option != "station_id":
                return None
                
            current_id = config.get_str("general", "station_id", fallback="")
            
            # If it's the same as current, no need to check
            if target_id == current_id:
                return None
                
            try:
                hardware_uuid = config.get_str("general", "hardware_uuid", fallback="")
                logger.info("VALIDATOR: Querying InfluxDB for ownership of station_id %s (our uuid: %s)...", target_id, hardware_uuid)
                # Try to get URL from config, then from env
                url = config.get_str("influxdb", "url", fallback="").strip("/")
                if not url:
                    url = os.getenv("INFLUX_URL", "").strip("/")
                
                query_url = url
                # No need to swap ports anymore as the proxy now handles both write and query
                if not url:
                    query_url = "http://influxdb:8086"
                
                token = config.get_str("influxdb", "token", fallback="") or os.getenv("INFLUX_TOKEN", "")
                org = config.get_str("influxdb", "org", fallback="") or os.getenv("INFLUX_ORG", "uclouvain")
                bucket = config.get_str("influxdb", "bucket", fallback="phenohive") or os.getenv("INFLUX_BUCKET", "phenohive")
                
                if not query_url or not token:
                    logger.error("VALIDATOR: Missing InfluxDB config: URL=%s, Token=%s", query_url, 'set' if token else 'unset')
                    return None
                    
                logger.info("VALIDATOR: Querying InfluxDB at %s/api/v2/query?org=%s", query_url, org)
                    
                # Query all distinct hardware_uuids that have claimed this target_id
                query = (
                    f'from(bucket: "{bucket}")\n'
                    f'  |> range(start: -1y)\n'
                    f'  |> filter(fn: (r) => r._measurement == "phenohive_measurements" and r.station_id == "{target_id}")\n'
                    f'  |> keep(columns: ["hardware_uuid"])\n'
                    f'  |> distinct(column: "hardware_uuid")'
                )
                
                import urllib.request
                import urllib.error
                import json
                import csv
                import io
                
                req_url = f"{query_url}/api/v2/query?org={org}"
                payload = {"query": query, "type": "flux"}
                req = urllib.request.Request(req_url, method="POST", data=json.dumps(payload).encode("utf-8"))
                req.add_header("Authorization", f"Token {token}")
                req.add_header("Content-Type", "application/json")
                req.add_header("Accept", "application/csv")
                
                with urllib.request.urlopen(req, timeout=3) as resp:
                    status = resp.getcode()
                    content = resp.read().decode("utf-8")
                    logger.info("VALIDATOR: Influx Response (Status: %d, first 50 chars): %s", status, content[:50])
                        
                    # Parse CSV properly to find the last hardware_uuid owner
                    f_csv = io.StringIO(content)
                    reader = csv.reader(f_csv)
                    other_owner_found = False
                    owner_uuid = None
                    
                    val_idx = 3 # Default Influx column
                    for row in reader:
                        if not row or (row[0] and row[0].startswith("#")):
                            continue
                        if "_value" in row:
                            try: val_idx = row.index("_value")
                            except: pass
                            continue
                        if len(row) > val_idx:
                            val = row[val_idx].strip().strip('"')
                            if val and val not in ["_value", "hardware_uuid", "result", "_result"]:
                                owner_uuid = val
                                if val != hardware_uuid:
                                    other_owner_found = True
                    
                    if other_owner_found:
                        logger.warning("VALIDATOR: Station ID %s is already owned by another device (%s)", target_id, owner_uuid)
                        return f"Station ID {target_id} is already owned by another device ({owner_uuid[:8]}...)"
                    
                    if owner_uuid == hardware_uuid:
                        logger.info("VALIDATOR: Station ID %s is already owned by US. Allowing reuse.", target_id)
                    else:
                        logger.info("VALIDATOR: Station ID %s is available (no previous owner found).", target_id)
                    
                    return None
            except urllib.error.HTTPError as e:
                logger.error("VALIDATOR: HTTP Error (%d): %s", e.code, e.read().decode("utf-8")[:100])
            except Exception as e:
                logger.error("VALIDATOR: Exception: %s", str(e))
                logger.warning("Could not validate station_id uniqueness against InfluxDB: %s", e)
            return None
        
        logger.info("Configuration validator initialized for station_id uniqueness check.")
        debug_ui_service = DebugUIService(
            config_path=Path(args.config),
            get_status=_runtime_status,
            write_token=debug_write_token,
            ta_password=debug_ta_password,
            allow_remote_writes=debug_allow_remote_writes,
            image_dir=Path(image_output_dir),
            camera_service=camera_service,
            image_processor=image_processor,
            sensors=sensors,
            config_validator=_validate_config_change,
        )
        debug_ui_service.start(host=debug_host, port=debug_port)

    setup_results = factory.setup_all(sensors)
    logger.info("Sensor setup results: %s", setup_results)

    if any(not v for v in setup_results.values()):
        startup_retry = factory.retry_failed_sensors(sensors, max_retries=3, retry_delay_s=2.0)
        if startup_retry:
            setup_results.update(startup_retry)
            logger.info("Sensor setup after startup retry: %s", startup_retry)

    data_manager = DataManager(
        csv_path=config.get_str("paths", "csv_path", fallback="data/measurements.csv") or "data/measurements.csv",
        influx_enabled=influx_enabled,
        influx_url=influx_url,
        influx_token=config.get_str("influxdb", "token", fallback="") or "",
        influx_org=config.get_str("influxdb", "org", fallback="") or "",
        influx_bucket=config.get_str("influxdb", "bucket", fallback="") or "",
        offline_queue_path=config.get_str("paths", "offline_queue_path", fallback="data/offline_queue.jsonl")
        or "data/offline_queue.jsonl",
        timeout_ms=config.get_int("influxdb", "timeout_ms", fallback=5000),
    )

    time_sync = build_time_sync_from_config(config)
    time_sync.sync_now()

    camera_service, image_processor = build_vision(config)
    if debug_ui_service is not None:
        debug_ui_service._camera_service = camera_service
        debug_ui_service._image_processor = image_processor

    led_service = None
    if config.get_bool("led_strip", "enabled", fallback=False):
        gpio_pin = config.get_int("led_strip", "gpio_pin", fallback=23)
        use_mock = config.get_bool("led_strip", "mock", fallback=False) or mock_mode
        led_service = MockLedStripService(gpio_pin=gpio_pin) if use_mock else LedStripService(gpio_pin=gpio_pin)
        if not led_service.setup():
            logger.warning("LED strip setup failed — camera will operate without LED")
            led_service = None

    publish_interval_s = max(1, config.get_int("general", "measurement_interval_s", fallback=60))
    default_sensor_samples = max(1, config.get_int("sampling", "default_samples", fallback=1))
    collection_interval_s = max(
        1,
        config.get_int(
            "sampling",
            "collection_interval_s",
            fallback=max(1, publish_interval_s // default_sensor_samples),
        ),
    )
    publish_interval_s = max(
        collection_interval_s,
        config.get_int("sampling", "publish_interval_s", fallback=publish_interval_s),
    )
    sample_interval_s = max(0.0, config.get_float("sampling", "sample_interval_ms", fallback=200.0) / 1000.0)
    outlier_method = (config.get_str("sampling", "outlier_method", fallback="mad") or "mad").strip().lower()
    mad_threshold = max(0.1, config.get_float("sampling", "mad_threshold", fallback=3.0))
    trim_ratio = min(max(config.get_float("sampling", "trim_ratio", fallback=0.2), 0.0), 0.49)
    min_success_ratio = min(max(config.get_float("sampling", "min_success_ratio", fallback=0.6), 0.0), 1.0)
    failed_sample_policy = (
        config.get_str("sampling", "failed_sample_policy", fallback="mark_only") or "mark_only"
    ).strip().lower()
    sensor_failed_sample_policies = {
        sensor_key: (
            config.get_str(
                "sampling",
                f"{sensor_key}_failed_sample_policy",
                fallback="carry_forward" if sensor_key == "scale_hx711" else failed_sample_policy,
            )
            or ("carry_forward" if sensor_key == "scale_hx711" else failed_sample_policy)
        ).strip().lower()
        for sensor_key in sensors
    }
    max_cv_for_confidence = max(0.0, config.get_float("sampling", "max_cv_for_confidence", fallback=0.35))

    calibration_enabled = config.get_bool("calibration", "enabled", fallback=True)
    calibration_interval_h = max(1.0, config.get_float("calibration", "interval_hours", fallback=168.0))
    calibration_drift_warn_pct = {
        "scale_hx711": max(0.1, config.get_float("calibration", "scale_hx711_drift_pct_warn", fallback=5.0)),
    }

    sensor_sample_counts = {
        sensor_key: max(
            1,
            config.get_int(
                "sampling",
                f"{sensor_key}_samples",
                fallback=1 if sensor_key == "scale_hx711" else default_sensor_samples,
            ),
        )
        for sensor_key in sensors
    }
    last_good_payloads: dict[str, dict[str, Any]] = {}
    calibration_state: dict[str, dict[str, Any]] = {}
    image_output_dir = config.get_str("camera", "image_output_dir", fallback="data/images") or "data/images"
    warmup_seconds = max(0.0, config.get_float("camera", "warmup_seconds", fallback=7.0))
    timeout_seconds = max(1.0, config.get_float("camera", "timeout_seconds", fallback=16.0))
    camera_publish_interval = max(1, config.get_int("camera", "interval_publishes", fallback=144))

    max_recent_captures = 3

    sensor_retry_interval_s = max(30.0, config.get_float("sampling", "sensor_retry_interval_s", fallback=60.0))
    sensor_failure_restart_min = max(5.0, config.get_float("sampling", "sensor_failure_restart_min", fallback=30.0))
    # Sensors that trigger a service restart if they stay broken beyond the threshold.
    # Scale is excluded: a missing weight is less critical than missing climate/light data.
    critical_sensors = frozenset(
        s.strip() for s in
        (config.get_str("sampling", "critical_sensors", fallback="sht35,tcs3448") or "sht35,tcs3448").split(",")
        if s.strip()
    )
    sensor_error_since: dict[str, float] = {}

    logger.info("Time sync diagnostics: %s", time_sync.diagnostics())

    collected_records: list[dict[str, Any]] = []
    publish_count = 0
    next_collection_due = time.monotonic()
    next_publish_due = next_collection_due if args.once else next_collection_due + publish_interval_s
    next_sensor_retry_due = time.monotonic() + sensor_retry_interval_s

    while True:
        published_once = False
        try:
            current_mono = time.monotonic()

            if current_mono >= next_sensor_retry_due:
                retry_results = factory.retry_failed_sensors(sensors, max_retries=1, retry_delay_s=0.0)
                if retry_results:
                    setup_results.update(retry_results)
                    logger.info("Sensor recovery: %s", retry_results)

                for key, sensor in sensors.items():
                    if sensor.status != SensorStatus.READY:
                        if key not in sensor_error_since:
                            sensor_error_since[key] = current_mono
                            logger.warning("Sensor %s entered persistent ERROR state — tracking for auto-restart", key)
                    else:
                        if key in sensor_error_since:
                            logger.info(
                                "Sensor %s recovered after %.0fs in ERROR state",
                                key, current_mono - sensor_error_since[key],
                            )
                        sensor_error_since.pop(key, None)

                for key in critical_sensors:
                    error_start = sensor_error_since.get(key)
                    if error_start is not None:
                        error_min = (current_mono - error_start) / 60.0
                        if error_min >= sensor_failure_restart_min:
                            logger.error(
                                "Sensor %s has been in ERROR for %.1f min (threshold %.0f min) — "
                                "triggering service restart via os._exit(1)",
                                key, error_min, sensor_failure_restart_min,
                            )
                            os._exit(1)

                next_sensor_retry_due = current_mono + sensor_retry_interval_s

            if current_mono >= next_collection_due:
                if led_service is not None and led_service.active.is_set():
                    logger.info("Skipping sensor collection — LED active")
                    while next_collection_due <= current_mono:
                        next_collection_due += collection_interval_s
                else:
                    if time_sync.is_resync_due():
                        time_sync.sync_now()

                    raw_record = collect_sensor_data(
                        sensors,
                        now_provider=time_sync.now_utc,
                        station_id=station_id,
                        hardware_uuid=hardware_uuid,
                        sensor_sample_counts=sensor_sample_counts,
                        sample_interval_s=sample_interval_s,
                        outlier_method=outlier_method,
                        mad_threshold=mad_threshold,
                        trim_ratio=trim_ratio,
                        min_success_ratio=min_success_ratio,
                        failed_sample_policy=failed_sample_policy,
                        sensor_failed_sample_policies=sensor_failed_sample_policies,
                        max_cv_for_confidence=max_cv_for_confidence,
                        last_good_payloads=last_good_payloads,
                        plausibility_ranges=PLAUSIBLE_RANGES,
                        calibration_enabled=calibration_enabled,
                        calibration_interval_h=calibration_interval_h,
                        calibration_state=calibration_state,
                        calibration_reference_fields=CALIBRATION_REFERENCE_FIELDS,
                        calibration_drift_warn_pct=calibration_drift_warn_pct,
                    )
                    latest_raw_sample = raw_record
                    collected_records.append(raw_record)

                    while next_collection_due <= current_mono:
                        next_collection_due += collection_interval_s

                    logger.info(
                        "Collected sample %s/%s in current publish window",
                        len(collected_records),
                        max(1, int(round(publish_interval_s / collection_interval_s))),
                    )

            should_publish = bool(collected_records) and (current_mono >= next_publish_due or args.once)
            if should_publish:
                publish_record = _smooth_time_window_records(
                    records=collected_records,
                    now_provider=time_sync.now_utc,
                    outlier_method=outlier_method,
                    mad_threshold=mad_threshold,
                    trim_ratio=trim_ratio,
                )

                publish_count += 1
                should_capture = (
                    camera_service is not None
                    and image_processor is not None
                    and (args.once or publish_count % camera_publish_interval == 0)
                )

                # Capture image in background to avoid blocking the main loop (prevents dashboard blackout)
                if should_capture:
                    target_record = publish_record # Close over the current record

                    def _capture_task():
                        nonlocal latest_vision_capture
                        try:
                            vision_payload = image_processor.capture_and_process(
                                camera_service=camera_service,
                                output_dir=image_output_dir,
                                warmup_seconds=config.get_int("camera", "warmup_seconds", fallback=7),
                                timeout_seconds=config.get_float("camera", "timeout_seconds", fallback=45.0),
                                led=led_service,
                            )
                            # Update the record in memory so the dashboard sees it on next poll
                            target_record.update({f"vision_{k}": v for k, v in vision_payload.items()})
                            # Keep the latest vision result permanently accessible for the dashboard
                            latest_vision_capture = {
                                "timestamp": target_record["timestamp"],
                                **{f"vision_{k}": v for k, v in vision_payload.items()},
                            }

                            # Construct and persist a dedicated vision record using the same timestamp and identity tags.
                            # InfluxDB will automatically merge these fields into the existing measurement point.
                            vision_record = {
                                "timestamp": target_record["timestamp"],
                                "station_id": target_record.get("station_id"),
                                "hardware_uuid": target_record.get("hardware_uuid"),
                            }
                            vision_record.update({f"vision_{k}": v for k, v in vision_payload.items()})
                            data_manager.persist_and_send(vision_record)
                        except Exception as exc:
                            logger.exception("Background camera capture failed: %s", exc)


                    threading.Thread(target=_capture_task, daemon=True).start()

                data_manager.persist_and_send(publish_record)
                capture_timestamp = str(publish_record.get("timestamp", datetime.now(UTC).isoformat()))
                latest_cycle["last_cycle_utc"] = capture_timestamp
                latest_cycle["last_cycle_ok"] = True
                latest_cycle["last_error"] = None

                recent_captures.append(publish_record)
                if len(recent_captures) > max_recent_captures:
                    recent_captures.pop(0)

                logger.info(
                    "Measurement publish completed with %s collected samples",
                    publish_record.get("sampling_window_records", 0),
                )

                collected_records = []
                while next_publish_due <= current_mono:
                    next_publish_due += publish_interval_s
                published_once = True
        except Exception as exc:  # noqa: BLE001
            latest_cycle["last_cycle_utc"] = datetime.now(UTC).isoformat()
            latest_cycle["last_cycle_ok"] = False
            latest_cycle["last_error"] = str(exc)
            logger.exception("Measurement cycle failed but loop continues: %s", exc)
            if args.once:
                break

        if args.once and published_once:
            break

        sleep_targets = [next_collection_due, next_sensor_retry_due]
        if collected_records:
            sleep_targets.append(next_publish_due)
        sleep_until = min(sleep_targets)
        sleep_time = max(0.0, sleep_until - time.monotonic())
        if sleep_time > 0.0:
            time.sleep(sleep_time)


if __name__ == "__main__":
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    main()
