"""Tests for CSV persistence and offline queue handling."""

from __future__ import annotations

import csv
import json
import threading
from pathlib import Path

from src.core.data_manager import DataManager


def make_local_manager(tmp_path: Path) -> DataManager:
    """Build a DataManager with Influx disabled (CSV-only) for archive tests."""
    return DataManager(
        csv_path=str(tmp_path / "measurements.csv"),
        influx_enabled=False,
        influx_url="http://localhost:8086",
        influx_token="",
        influx_org="",
        influx_bucket="",
        offline_queue_path=str(tmp_path / "offline_queue.jsonl"),
    )


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    """Return CSV rows as dictionaries."""
    with path.open("r", encoding="utf-8", newline="") as csv_file:
        return list(csv.DictReader(csv_file))


def test_persist_writes_csv_when_influx_disabled(tmp_path: Path) -> None:
    """CSV writes should still work when Influx is disabled."""
    csv_path = tmp_path / "measurements.csv"
    queue_path = tmp_path / "offline_queue.jsonl"

    manager = DataManager(
        csv_path=str(csv_path),
        influx_enabled=False,
        influx_url="http://localhost:8086",
        influx_token="",
        influx_org="",
        influx_bucket="",
        offline_queue_path=str(queue_path),
    )

    manager.persist_and_send({"sensor": "mock", "value": 1.5})

    assert csv_path.exists()
    rows = read_csv_rows(csv_path)
    assert len(rows) == 1
    assert rows[0]["sensor"] == "mock"
    assert rows[0]["value"] == "1.5"


def test_failed_send_is_buffered_to_offline_queue(tmp_path: Path, monkeypatch) -> None:
    """Failed sends should be buffered in the offline queue."""
    csv_path = tmp_path / "measurements.csv"
    queue_path = tmp_path / "offline_queue.jsonl"

    manager = DataManager(
        csv_path=str(csv_path),
        influx_enabled=True,
        influx_url="http://localhost:8086",
        influx_token="token",
        influx_org="org",
        influx_bucket="bucket",
        offline_queue_path=str(queue_path),
    )

    monkeypatch.setattr(manager, "_send_record", lambda record: False)
    manager.persist_and_send({"sensor": "mock", "value": 2.0})

    assert queue_path.exists()
    lines = queue_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["value"] == 2.0


def test_successful_send_flushes_existing_offline_queue(tmp_path: Path, monkeypatch) -> None:
    """Successful sends should flush already queued records."""
    csv_path = tmp_path / "measurements.csv"
    queue_path = tmp_path / "offline_queue.jsonl"
    queue_path.write_text(
        "\n".join(
            [
                json.dumps({"sensor": "queued", "value": 10.0}),
                json.dumps({"sensor": "queued", "value": 11.0}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    manager = DataManager(
        csv_path=str(csv_path),
        influx_enabled=True,
        influx_url="http://localhost:8086",
        influx_token="token",
        influx_org="org",
        influx_bucket="bucket",
        offline_queue_path=str(queue_path),
    )

    calls: list[dict[str, object]] = []

    def fake_send(record: dict[str, object]) -> bool:
        calls.append(record)
        return True

    monkeypatch.setattr(manager, "_send_record", fake_send)

    manager.persist_and_send({"sensor": "current", "value": 99.0})

    assert len(calls) == 3
    remaining = queue_path.read_text(encoding="utf-8").strip()
    assert remaining == ""


def test_persist_adds_timestamp_when_missing(tmp_path: Path) -> None:
    """CSV should include a generated timestamp when the record has none."""
    csv_path = tmp_path / "measurements.csv"
    queue_path = tmp_path / "offline_queue.jsonl"

    manager = DataManager(
        csv_path=str(csv_path),
        influx_enabled=False,
        influx_url="http://localhost:8086",
        influx_token="",
        influx_org="",
        influx_bucket="",
        offline_queue_path=str(queue_path),
    )

    manager.persist_and_send({"sensor": "mock"})

    rows = read_csv_rows(csv_path)
    assert len(rows) == 1
    assert "timestamp" in rows[0]
    assert rows[0]["timestamp"]  # non-empty


def test_malformed_offline_queue_line_is_skipped(tmp_path: Path, monkeypatch) -> None:
    """A malformed JSONL line in the offline queue should be skipped without crashing."""
    csv_path = tmp_path / "measurements.csv"
    queue_path = tmp_path / "offline_queue.jsonl"
    queue_path.write_text("not valid json\n", encoding="utf-8")

    manager = DataManager(
        csv_path=str(csv_path),
        influx_enabled=True,
        influx_url="http://localhost:8086",
        influx_token="token",
        influx_org="org",
        influx_bucket="bucket",
        offline_queue_path=str(queue_path),
    )

    calls: list[dict] = []

    def fake_send(record: dict) -> bool:
        calls.append(record)
        return True

    monkeypatch.setattr(manager, "_send_record", fake_send)
    manager.persist_and_send({"sensor": "ok", "value": 1.0})

    # Malformed line was skipped; only the current record was sent.
    assert len(calls) == 1
    assert calls[0]["sensor"] == "ok"
    remaining = queue_path.read_text(encoding="utf-8").strip()
    assert remaining == ""


def test_csv_heterogeneous_schemas_stay_aligned(tmp_path: Path) -> None:
    """Records with different key sets must not corrupt column alignment.

    Reproduces the real station behaviour: a sensor record, then a separately
    written vision_* record, then a later record that gained a calibration
    field. Every value must read back under its own column.
    """
    csv_path = tmp_path / "measurements.csv"
    manager = make_local_manager(tmp_path)

    manager.persist_and_send(
        {"timestamp": "T1", "station_id": "1", "air_temperature_c": 22.5, "air_humidity_pct": 55.0}
    )
    manager.persist_and_send(
        {"timestamp": "T2", "station_id": "1", "vision_growth": 3.4, "vision_status": "ok"}
    )
    manager.persist_and_send(
        {"timestamp": "T3", "station_id": "1", "air_temperature_c": 23.0, "scale_hx711_drift_pct": 1.2}
    )

    rows = read_csv_rows(csv_path)
    assert len(rows) == 3

    # No ragged rows: DictReader never falls back to the None restkey.
    assert all(None not in row for row in rows)

    # Each value lands under the correct column (the old bug put "T2" in temperature).
    assert rows[0]["air_temperature_c"] == "22.5"
    assert rows[0]["air_humidity_pct"] == "55.0"
    assert rows[0]["vision_growth"] == ""  # back-filled, not present in record 1

    assert rows[1]["vision_growth"] == "3.4"
    assert rows[1]["vision_status"] == "ok"
    assert rows[1]["air_temperature_c"] == ""  # sensor fields absent for the vision row

    assert rows[2]["air_temperature_c"] == "23.0"
    assert rows[2]["scale_hx711_drift_pct"] == "1.2"
    assert rows[2]["air_humidity_pct"] == ""


def test_csv_new_column_backfills_existing_rows(tmp_path: Path) -> None:
    """Introducing a new key rewrites the header and back-fills earlier rows."""
    csv_path = tmp_path / "measurements.csv"
    manager = make_local_manager(tmp_path)

    manager.persist_and_send({"timestamp": "T1", "a": 1})
    manager.persist_and_send({"timestamp": "T2", "a": 2, "b": 9})

    rows = read_csv_rows(csv_path)
    assert [r["a"] for r in rows] == ["1", "2"]
    assert rows[0]["b"] == ""   # column added later, back-filled for the first row
    assert rows[1]["b"] == "9"


def test_csv_concurrent_writes_stay_consistent(tmp_path: Path) -> None:
    """Concurrent persist() calls with differing schemas stay aligned and complete.

    Mirrors the main publish loop and the background camera thread writing to the
    same DataManager at once.
    """
    csv_path = tmp_path / "measurements.csv"
    manager = make_local_manager(tmp_path)

    thread_count = 20

    def worker(i: int) -> None:
        manager.persist_and_send({"timestamp": f"T{i}", "idx": i, f"field_{i}": i * 10})

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(thread_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    rows = read_csv_rows(csv_path)
    assert len(rows) == thread_count
    assert all(None not in row for row in rows)  # no ragged/misaligned rows

    by_idx = {row["idx"]: row for row in rows}
    assert len(by_idx) == thread_count  # every write landed exactly once
    for i in range(thread_count):
        row = by_idx[str(i)]
        assert row[f"field_{i}"] == str(i * 10)  # each row's unique field is correct
