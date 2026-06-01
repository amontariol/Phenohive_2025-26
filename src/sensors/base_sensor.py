"""Base sensor contracts and shared status types."""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum
from typing import Any


class SensorStatus(str, Enum):
    """Common runtime states for sensors."""

    UNINITIALIZED = "uninitialized"
    READY = "ready"
    ERROR = "error"


class BaseSensor(ABC):
    """Common interface shared by all real and mock sensors."""

    name: str
    status: SensorStatus
    last_error: str | None

    def __init__(self, name: str) -> None:
        """Initialize shared sensor metadata."""
        self.name = name
        self.status = SensorStatus.UNINITIALIZED
        self.last_error = None

    @abstractmethod
    def setup(self) -> bool:
        """Initialize sensor communication and internal state."""

    @abstractmethod
    def read_data(self) -> dict[str, Any]:
        """Read one sample and return a payload dictionary."""

    def mark_error(self, message: str) -> None:
        """Mark sensor as failed and store the latest error."""
        self.status = SensorStatus.ERROR
        self.last_error = message

    def mark_ready(self) -> None:
        """Mark sensor as ready and clear previous errors."""
        self.status = SensorStatus.READY
        self.last_error = None

    def update_setting(self, key: str, value: Any) -> None:
        """Update a specific sensor setting at runtime. No-op by default."""
        pass
