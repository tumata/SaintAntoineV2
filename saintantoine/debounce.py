"""Hold-to-trigger press detection (D3) — the phantom-press defense.

A press is accepted only if the input reads pressed *continuously* over a
sampling window, so a brief EMI glitch (relay switching noise, floating-input
pickup) cannot survive. On top of that: a minimum interval between accepted
presses, a rapid-fire lockout, and wait-for-release re-arming.

This applies to the BUTTON INPUT ONLY — relays are a pure output (SPECS §5).

Clock and sleep are injectable so unit tests run on virtual time.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Callable, Optional

log = logging.getLogger(__name__)


class PressDetector:
    def __init__(
        self,
        is_pressed: Callable[[], bool],
        on_press: Callable[[], None],
        hold_window_ms: int = 50,
        sample_interval_ms: int = 5,
        min_press_interval_ms: int = 300,
        rapidfire_count: int = 3,
        rapidfire_window_s: float = 1.0,
        rapidfire_cooldown_s: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        stop_event: Optional[threading.Event] = None,
    ):
        self.is_pressed = is_pressed
        self.on_press = on_press
        self.hold_window_s = hold_window_ms / 1000.0
        self.sample_interval_s = max(0.001, sample_interval_ms / 1000.0)
        self.min_press_interval_s = min_press_interval_ms / 1000.0
        self.rapidfire_count = rapidfire_count
        self.rapidfire_window_s = rapidfire_window_s
        self.rapidfire_cooldown_s = rapidfire_cooldown_s
        self.clock = clock
        self.sleep = sleep
        self.stop_event = stop_event or threading.Event()
        self._last_accepted: Optional[float] = None
        self._recent: deque = deque()

    # -- main loop -----------------------------------------------------------

    def run(self) -> None:
        log.info("Button press detector started (hold window %.0f ms).", self.hold_window_s * 1000)
        while not self.stop_event.is_set():
            try:
                self.poll_once()
            except Exception:
                log.exception("Error in press detector loop")
                self.sleep(0.1)
        log.info("Button press detector stopped.")

    def poll_once(self) -> None:
        if not self.is_pressed():
            self.sleep(self.sample_interval_s)
            return
        self._handle_candidate()

    # -- internals -----------------------------------------------------------

    def _handle_candidate(self) -> None:
        # Hold-to-trigger: input must stay pressed across the whole window
        samples = max(1, round(self.hold_window_s / self.sample_interval_s))
        for _ in range(samples):
            if self.stop_event.is_set():
                return
            if not self.is_pressed():
                log.info("Rejected press: glitch (did not survive %.0f ms hold window).",
                         self.hold_window_s * 1000)
                return
            self.sleep(self.sample_interval_s)

        now = self.clock()

        if self._last_accepted is not None and (now - self._last_accepted) < self.min_press_interval_s:
            log.info("Rejected press: %.0f ms since last accepted press (min %.0f ms).",
                     (now - self._last_accepted) * 1000, self.min_press_interval_s * 1000)
            self._wait_release()
            return

        # Rapid-fire lockout: too many accepted presses in a short span
        while self._recent and now - self._recent[0] > self.rapidfire_window_s:
            self._recent.popleft()
        if len(self._recent) >= self.rapidfire_count:
            log.warning("Rapid-fire lockout: %d presses within %.1f s — cooling down %.1f s.",
                        len(self._recent), self.rapidfire_window_s, self.rapidfire_cooldown_s)
            self._recent.clear()
            self.sleep(self.rapidfire_cooldown_s)
            self._wait_release()
            return

        self._last_accepted = now
        self._recent.append(now)
        log.info("Button press accepted.")
        try:
            self.on_press()
        except Exception:
            log.exception("on_press handler failed")
        self._wait_release()

    def _wait_release(self) -> None:
        while self.is_pressed() and not self.stop_event.is_set():
            self.sleep(self.sample_interval_s)
