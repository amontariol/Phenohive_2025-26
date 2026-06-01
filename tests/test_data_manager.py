"""Tests for CSV persistence and offline queue handling."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from src.core.data_manager import DataManager


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
