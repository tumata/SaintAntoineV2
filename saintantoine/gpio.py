"""GPIO backends: real (gpiozero) and mock.

The 3 relays are always switched as one group (SPECS §3) and carry no
triggering logic of their own — pure output.
"""

from __future__ import annotations

import abc
import logging
import threading
from typing import List

from .config import Config

log = logging.getLogger(__name__)


class GpioBackend(abc.ABC):
    @abc.abstractmethod
    def relays_on(self) -> None: ...

    @abc.abstractmethod
    def relays_off(self) -> None: ...

    @abc.abstractmethod
    def relay_states(self) -> List[bool]: ...

    @abc.abstractmethod
    def is_button_pressed(self) -> bool: ...

    @abc.abstractmethod
    def close(self) -> None: ...


class MockGpio(GpioBackend):
    """In-memory GPIO: relay state flags + an injectable button state."""

    def __init__(self, relay_count: int = 3):
        self._relays = [False] * relay_count
        self.button_pressed = False
        self._lock = threading.Lock()

    def relays_on(self) -> None:
        with self._lock:
            self._relays = [True] * len(self._relays)
        log.info("Relays ON (mock).")

    def relays_off(self) -> None:
        with self._lock:
            self._relays = [False] * len(self._relays)
        log.info("Relays OFF (mock).")

    def relay_states(self) -> List[bool]:
        with self._lock:
            return list(self._relays)

    def is_button_pressed(self) -> bool:
        return self.button_pressed

    def close(self) -> None:
        self.relays_off()

    # Test helpers
    def press(self) -> None:
        self.button_pressed = True

    def release(self) -> None:
        self.button_pressed = False


class RealGpio(GpioBackend):
    """gpiozero-backed button + relay group (imported lazily: Pi only)."""

    def __init__(self, cfg: Config):
        from gpiozero import Button, OutputDevice  # imported here: not present off-Pi

        self._button = Button(
            cfg.button_pin,
            pull_up=cfg.button_pull_up,
            bounce_time=cfg.gpiozero_bounce_time_s or None,
        )
        self._relays = [
            OutputDevice(
                pin,
                active_high=cfg.relay_active_high,
                initial_value=cfg.relay_initial_value,
            )
            for pin in cfg.relay_pins
        ]
        self._lock = threading.Lock()
        log.info("Real GPIO ready: button on GPIO%d, relays on %s.",
                 cfg.button_pin, cfg.relay_pins)

    def relays_on(self) -> None:
        with self._lock:
            for r in self._relays:
                r.on()
        log.info("Relays ON.")

    def relays_off(self) -> None:
        with self._lock:
            for r in self._relays:
                r.off()
        log.info("Relays OFF.")

    def relay_states(self) -> List[bool]:
        with self._lock:
            return [bool(r.value) for r in self._relays]

    def is_button_pressed(self) -> bool:
        return self._button.is_pressed

    def close(self) -> None:
        try:
            self.relays_off()
        finally:
            for r in self._relays:
                r.close()
            self._button.close()
