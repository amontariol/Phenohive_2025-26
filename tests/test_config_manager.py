"""Tests around config parsing and fallback behavior."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.core.config_manager import ConfigManager


def write_ini(path: Path, content: str) -> None:
    """Write INI text to disk."""
    path.write_text(content.strip() + "\n", encoding="utf-8")


def test_load_missing_file_raises(tmp_path: Path) -> None:
    """Missing config files should raise clearly."""
    cfg = ConfigManager(config_path=tmp_path / "missing.ini")
    with pytest.raises(FileNotFoundError):
        cfg.load()


def test_mock_mode_resolution_and_sensor_override(tmp_path: Path) -> None:
    """Per-sensor mode should override global mock mode."""
    ini_path = tmp_path / "config.ini"
    write_ini(
        ini_path,
        """
        [general]
        mock_mode = false

        [sensors]
        sht35 = mock
        tcs3448 = real
        """,
    )

    cfg = ConfigManager(config_path=ini_path)
    cfg.load()

    assert cfg.is_mock_mode() is False
    assert cfg.use_mock_for_sensor("sht35") is True
    assert cfg.use_mock_for_sensor("tcs3448") is False
    assert cfg.use_mock_for_sensor("scale_hx711") is False  # no entry → falls back to global


def test_use_mock_falls_back_on_invalid_sensor_value(tmp_path: Path) -> None:
    """Invalid sensor mode values should fall back to global mode."""
    ini_path = tmp_path / "config.ini"
    write_ini(
        ini_path,
        """
        [general]
        mock_mode = true

        [sensors]
        sht35 = maybe
        """,
    )

    cfg = ConfigManager(config_path=ini_path)
    cfg.load()

    assert cfg.use_mock_for_sensor("sht35") is True


def test_typed_getters_return_fallback_on_invalid_values(tmp_path: Path) -> None:
    """Bad typed values should not crash and should use fallbacks."""
    ini_path = tmp_path / "config.ini"
    write_ini(
        ini_path,
        """
        [section]
        bad_int = abc
        bad_float = nope
        bad_bool = maybe
        """,
    )

    cfg = ConfigManager(config_path=ini_path)
    cfg.load()

    assert cfg.get_int("section", "bad_int", fallback=7) == 7
    assert cfg.get_float("section", "bad_float", fallback=1.25) == 1.25
    assert cfg.get_bool("section", "bad_bool", fallback=True) is True


def test_require_keys_reports_missing_entries(tmp_path: Path) -> None:
    """Missing entries should be reported as [section].option."""
    ini_path = tmp_path / "config.ini"
    write_ini(
        ini_path,
        """
        [alpha]
        one = 1
        """,
    )

    cfg = ConfigManager(config_path=ini_path)
    cfg.load()

    missing = cfg.require_keys(
        {
            "alpha": ["one", "two"],
            "beta": ["x"],
        }
    )

    assert "[alpha].two" in missing
    assert "[beta].x" in missing


def test_env_var_overrides_config_value(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """SECTION_OPTION env vars should override the corresponding INI value."""
    ini_path = tmp_path / "config.ini"
    write_ini(ini_path, "[influxdb]\ntoken = file_token\n")

    cfg = ConfigManager(config_path=ini_path)
    cfg.load()

    monkeypatch.setenv("INFLUXDB_TOKEN", "env_token")
    assert cfg.get_str("influxdb", "token") == "env_token"


def test_influx_alias_overrides_influxdb_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """INFLUX_* env vars should be accepted as aliases for [influxdb] keys."""
    ini_path = tmp_path / "config.ini"
    write_ini(ini_path, "[influxdb]\ntoken = file_token\n")

    cfg = ConfigManager(config_path=ini_path)
    cfg.load()

    monkeypatch.setenv("INFLUX_TOKEN", "alias_token")
    assert cfg.get_str("influxdb", "token") == "alias_token"


def test_set_str_and_save_persists_value(tmp_path: Path) -> None:
    """set_str + save should make the new value readable after a fresh load."""
    ini_path = tmp_path / "config.ini"
    write_ini(ini_path, "[general]\nstation_id = 1\n")

    cfg = ConfigManager(config_path=ini_path)
    cfg.load()
    cfg.set_str("general", "station_id", "42")
    cfg.save()

    cfg2 = ConfigManager(config_path=ini_path)
    cfg2.load()
    assert cfg2.get_str("general", "station_id") == "42"


def test_ensure_uuid_generates_and_persists(tmp_path: Path) -> None:
    """ensure_uuid should generate a UUID, write it to disk, and return the same value on reload."""
    ini_path = tmp_path / "config.ini"
    write_ini(ini_path, "[general]\nstation_id = 1\n")

    cfg = ConfigManager(config_path=ini_path)
    cfg.load()

    uuid1 = cfg.ensure_uuid()
    assert len(uuid1) == 36  # standard UUID string length

    cfg2 = ConfigManager(config_path=ini_path)
    cfg2.load()
    uuid2 = cfg2.ensure_uuid()
    assert uuid1 == uuid2


def test_reload_picks_up_disk_changes(tmp_path: Path) -> None:
    """reload() should discard in-memory state and re-read from disk."""
    ini_path = tmp_path / "config.ini"
    write_ini(ini_path, "[general]\nstation_id = 1\n")

    cfg = ConfigManager(config_path=ini_path)
    cfg.load()
    assert cfg.get_str("general", "station_id") == "1"

    write_ini(ini_path, "[general]\nstation_id = 99\n")
    cfg.reload()
    assert cfg.get_str("general", "station_id") == "99"
