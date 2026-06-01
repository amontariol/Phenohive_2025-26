"""LED strip control via KY-019 relay (active HIGH on GPIO)."""

from __future__ import annotations

import logging
import threading

LOGGER = logging.getLogger(__name__)


class LedStripService:
    """Controls a 5V LED strip through a KY-019 relay on a BCM GPIO pin."""

    def __init__(self, gpio_pin: int = 23) -> None:
        self._pin = gpio_pin
        self._gpio = None
        self.active = threading.Event()

    def setup(self) -> bool:
        try:
            import RPi.GPIO as GPIO  # noqa: N813

            self._gpio = GPIO
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(self._pin, GPIO.OUT)
            GPIO.output(self._pin, GPIO.LOW)
            LOGGER.info("LED strip ready on GPIO %d", self._pin)
            return True
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("LED strip setup failed (continuing without): %s", exc)
            self._gpio = None
            return False

    def on(self) -> None:
        if self._gpio is not None:
            self._gpio.output(self._pin, self._gpio.HIGH)
        self.active.set()

    def off(self) -> None:
        if self._gpio is not None:
            self._gpio.output(self._pin, self._gpio.LOW)
        self.active.clear()

    def cleanup(self) -> None:
        if self._gpio is not None:
            try:
                self._gpio.output(self._pin, self._gpio.LOW)
                self._gpio.cleanup(self._pin)
            except Exception:  # noqa: BLE001
                pass
        self.active.clear()


class MockLedStripService:
    """Logs on/off calls without touching GPIO — for dev and test environments."""

    def __init__(self, gpio_pin: int = 23) -> None:
        self._pin = gpio_pin
        self.active = threading.Event()

    def setup(self) -> bool:
        LOGGER.info("MockLedStripService: setup on GPIO %d (no hardware)", self._pin)
        return True

    def on(self) -> None:
        LOGGER.debug("MockLedStripService: LED ON")
        self.active.set()

    def off(self) -> None:
        LOGGER.debug("MockLedStripService: LED OFF")
        self.active.clear()

    def cleanup(self) -> None:
        self.active.clear()
