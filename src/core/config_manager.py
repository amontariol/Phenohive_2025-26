"""Configuration loading and typed access helpers."""

from __future__ import annotations

import configparser
import logging
import os
import uuid
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)


class ConfigManager:
    """Wrap `configparser` with safer typed getters and fallbacks."""

    def __init__(self, config_path: str | Path = "config.ini", strict: bool = False) -> None:
        self._path = Path(config_path)
        self._parser = configparser.ConfigParser(strict=strict)
        self._parser.optionxform = str.lower

    @property
    def path(self) -> Path:
        """Path to the active config file."""
        return self._path

    def load(self) -> None:
        """Load configuration from disk."""
        if not self._path.exists():
            raise FileNotFoundError(f"Configuration file not found: {self._path}")

        read_files = self._parser.read(self._path)
        if not read_files:
            raise FileNotFoundError(f"Unable to read configuration file: {self._path}")

        LOGGER.info("Configuration loaded from %s", self._path)

    def reload(self) -> None:
        """Reload the INI file and discard old values."""
        self._parser.clear()
        self.load()

    def has_section(self, section: str) -> bool:
        """Check if a section exists."""
        return self._parser.has_section(section)

    def _env_candidates(self, section: str, option: str) -> list[str]:
        """Build env-var names that can override one config key."""
        normalized_section = "".join(ch if ch.isalnum() else "_" for ch in section.upper())
        normalized_option = "".join(ch if ch.isalnum() else "_" for ch in option.upper())

        candidates = [
            f"{normalized_section}_{normalized_option}",
            normalized_option,
        ]

        # Also accept INFLUX_* aliases for [influxdb] keys.
        if normalized_section.endswith("DB"):
            base_section = normalized_section[:-2]
            if base_section:
                candidates.append(f"{base_section}_{normalized_option}")

        return candidates

    def _get_env_override(self, section: str, option: str) -> str | None:
        """Return env override value when available."""
        for key in self._env_candidates(section, option):
            value = os.getenv(key)
            if value is not None:
                return value
        return None

    def get_all_env_overrides(self) -> dict[str, dict[str, str]]:
        """Return a dictionary of all active environment overrides for known sections/options."""
        overrides: dict[str, dict[str, str]] = {}
        for section in self._parser.sections():
            for option in self._parser.options(section):
                val = self._get_env_override(section, option)
                if val is not None:
                    overrides.setdefault(section, {})[option] = val
        return overrides

    def get_str(self, section: str, option: str, fallback: str | None = None) -> str | None:
        """Return a string setting, with fallback on errors."""
        env_override = self._get_env_override(section, option)
        if env_override is not None:
            return env_override

        try:
            return self._parser.get(section, option, fallback=fallback)
        except (configparser.Error, ValueError) as exc:
            LOGGER.warning(
                "Invalid config for [%s] %s, using fallback %r (%s)",
                section,
                option,
                fallback,
                exc,
            )
            return fallback

    def get_bool(self, section: str, option: str, fallback: bool = False) -> bool:
        """Return a boolean setting, with fallback on errors."""
        env_override = self._get_env_override(section, option)
        if env_override is not None:
            normalized = env_override.strip().lower()
            if normalized in {"1", "true", "yes", "on"}:
                return True
            if normalized in {"0", "false", "no", "off"}:
                return False
            LOGGER.warning(
                "Invalid boolean environment override for [%s] %s: %r. Using fallback/config value.",
                section,
                option,
                env_override,
            )

        try:
            return self._parser.getboolean(section, option, fallback=fallback)
        except (configparser.Error, ValueError) as exc:
            LOGGER.warning(
                "Invalid boolean config for [%s] %s, using fallback %r (%s)",
                section,
                option,
                fallback,
                exc,
            )
            return fallback

    def get_int(self, section: str, option: str, fallback: int = 0) -> int:
        """Return an integer setting, with fallback on errors."""
        env_override = self._get_env_override(section, option)
        if env_override is not None:
            try:
                return int(env_override, 0)
            except ValueError:
                LOGGER.warning(
                    "Invalid integer environment override for [%s] %s: %r. Using fallback/config value.",
                    section,
                    option,
                    env_override,
                )

        try:
            return self._parser.getint(section, option, fallback=fallback)
        except (configparser.Error, ValueError) as exc:
            LOGGER.warning(
                "Invalid integer config for [%s] %s, using fallback %r (%s)",
                section,
                option,
                fallback,
                exc,
            )
            return fallback

    def get_float(self, section: str, option: str, fallback: float = 0.0) -> float:
        """Return a float setting, with fallback on errors."""
        env_override = self._get_env_override(section, option)
        if env_override is not None:
            try:
                return float(env_override)
            except ValueError:
                LOGGER.warning(
                    "Invalid float environment override for [%s] %s: %r. Using fallback/config value.",
                    section,
                    option,
                    env_override,
                )

        try:
            return self._parser.getfloat(section, option, fallback=fallback)
        except (configparser.Error, ValueError) as exc:
            LOGGER.warning(
                "Invalid float config for [%s] %s, using fallback %r (%s)",
                section,
                option,
                fallback,
                exc,
            )
            return fallback

    def get_section_dict(self, section: str) -> dict[str, str]:
        """Return all key-value pairs for one section."""
        if not self._parser.has_section(section):
            return {}
        return {key: value for key, value in self._parser.items(section)}

    def as_dict(self) -> dict[str, dict[str, str]]:
        """Export the full config as nested dictionaries."""
        output: dict[str, dict[str, str]] = {}
        for section in self._parser.sections():
            output[section] = self.get_section_dict(section)
        return output

    def is_mock_mode(self) -> bool:
        """Return whether global mock mode is enabled."""
        if self.has_section("general"):
            return self.get_bool("general", "mock_mode", fallback=False)
        if self.has_section("station"):
            return self.get_bool("station", "mock_mode", fallback=False)
        return False

    def set_str(self, section: str, option: str, value: str) -> None:
        """Set a configuration value in memory."""
        if not self._parser.has_section(section):
            self._parser.add_section(section)
        self._parser.set(section, option, value)

    def save(self) -> None:
        """Write the current configuration back to disk."""
        with self._path.open("w", encoding="utf-8") as f:
            self._parser.write(f)
        LOGGER.info("Configuration saved to %s", self._path)

    def ensure_uuid(self) -> str:
        """Return the station UUID, generating and saving it if missing."""
        existing = self.get_str("general", "hardware_uuid")
        if existing:
            return existing

        new_uuid = str(uuid.uuid4())
        LOGGER.info("Generating new hardware UUID: %s", new_uuid)
        self.set_str("general", "hardware_uuid", new_uuid)
        self.save()
        return new_uuid

    def use_mock_for_sensor(self, sensor_key: str) -> bool:
        """Return whether one sensor should run in mock mode."""
        if self.has_section("sensors"):
            sensor_value = self.get_str("sensors", sensor_key, fallback=None)
            if sensor_value is not None:
                normalized = sensor_value.strip().lower()
                if normalized in {"1", "true", "yes", "on", "mock"}:
                    return True
                if normalized in {"0", "false", "no", "off", "real"}:
                    return False
                LOGGER.warning(
                    "Unsupported sensor mode value for key '%s': %r. Using global mode.",
                    sensor_key,
                    sensor_value,
                )
        return self.is_mock_mode()

    def require_keys(self, required: dict[str, list[str]]) -> list[str]:
        """Return missing required keys as `[section].option` strings."""
        missing: list[str] = []
        for section, options in required.items():
            if not self.has_section(section):
                missing.extend([f"[{section}].{option}" for option in options])
                continue
            for option in options:
                if self._get_env_override(section, option) is not None:
                    continue
                if not self._parser.has_option(section, option):
                    missing.append(f"[{section}].{option}")
        return missing


def load_config(config_path: str | Path = "config.ini") -> ConfigManager:
    """Load config and return a ready `ConfigManager`."""
    manager = ConfigManager(config_path=config_path)
    manager.load()
    return manager
