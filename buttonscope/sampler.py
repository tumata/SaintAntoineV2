"""Sampler thread, edge detection, ring buffer, counters, mock signal.

SPECS_BUTTONSCOPE §3, §4, §9. The sampler polls the raw line (no debounce,
no filtering) and stores *edges* — a square wave is fully described by its
transitions (BD7). Pulses away from the idle level shorter than `glitch_ms`
count as glitches; longer ones as presses.
"""

from __future__ import annotations

import abc
import logging
import threading
import time
from collections import deque
from typing import Callable, Deque, Optional, Tuple

log = logging.getLogger(__name__)

PULLS = ("down", "up", "none")

# (seq, monotonic timestamp, level-after-edge)
Edge = Tuple[int, float, int]


class InputSource(abc.ABC):
    """One observed line. read() returns the *electrical* level (HIGH=True)."""

    @abc.abstractmethod
    def read(self, now: float) -> bool: ...

    def close(self) -> None:  # pragma: no cover - trivial default
        pass


class RealInput(InputSource):
    """Raw line via gpiozero (lazy import, Pi only). No bounce_time — unfiltered."""

    def __init__(self, pin: int, pull: str):
        from gpiozero import DigitalInputDevice  # imported here: not present off-Pi

        pull_up = {"down": False, "up": True, "none": None}[pull]
        kwargs = {"pull_up": pull_up}
        if pull_up is None:
            kwargs["active_state"] = True
        self._dev = DigitalInputDevice(pin, **kwargs)
        # gpiozero reports the *active* state; pulled-up inputs are active-low,
        # so invert to recover the electrical level.
        self._invert = pull == "up"

    def read(self, now: float) -> bool:
        return bool(self._dev.value) != self._invert

    def close(self) -> None:
        self._dev.close()


class MockSignal(InputSource):
    """Deterministic synthetic waveform (§9), a pure function of time.

    6 s cycle: two "presses" (with a few ms of simulated contact bounce on
    each edge) and one 4 ms glitch, so the glitch highlighting and counters
    are visibly exercised off-Pi. Honors the pull: idle level matches it.
    """

    PERIOD = 6.0
    PRESSES = ((0.5, 1.2), (3.0, 3.8))
    GLITCH = (5.0, 5.004)
    BOUNCE_S = 0.006
    BOUNCE_STEP = 0.0015

    def __init__(self, pin: int, pull: str):
        self.pin = pin
        self.pull = pull

    def _active(self, now: float) -> bool:
        t = now % self.PERIOD
        if self.GLITCH[0] <= t < self.GLITCH[1]:
            return True
        base = any(start <= t < end for start, end in self.PRESSES)
        for start, end in self.PRESSES:
            for edge in (start, end):
                d = t - edge
                if 0 <= d < self.BOUNCE_S and int(d / self.BOUNCE_STEP) % 2 == 1:
                    return not base
        return base

    def read(self, now: float) -> bool:
        # Active means "pressed"; with a pull-up that is electrically LOW.
        return self._active(now) != (self.pull == "up")


class Sampler:
    """Owns the input source; polls, detects edges, classifies pulses.

    Thread-safe: tick() (polling thread), set_input() / snapshots (web thread)
    all serialize on one lock, so a pin swap can never interleave with a read.
    """

    def __init__(
        self,
        source_factory: Callable[[int, str], InputSource],
        pin: int,
        pull: str,
        sample_hz: float = 1000.0,
        glitch_ms: float = 20.0,
        retention_s: float = 15.0,
        time_fn: Callable[[], float] = time.monotonic,
    ):
        if pull not in PULLS:
            raise ValueError(f"pull must be one of {PULLS}")
        self._factory = source_factory
        self._lock = threading.Lock()
        self.sample_hz = sample_hz
        self.glitch_ms = glitch_ms
        self._glitch_s = glitch_ms / 1000.0
        self._retention_s = retention_s
        self._time_fn = time_fn

        self._pin = pin
        self._pull = pull
        self._error = ""
        self._edges: Deque[Edge] = deque()
        self._seq = 0
        self._epoch = 0
        self._level: Optional[int] = None
        self._nonidle_since: Optional[float] = None
        self._counters = {"edges": 0, "glitches": 0, "presses": 0}
        self._achieved_hz = 0.0
        self._rate_reads = 0
        self._rate_t0: Optional[float] = None

        # May raise (pin busy) — the caller turns that into the startup message.
        self._source: Optional[InputSource] = source_factory(pin, pull)

    # ------------------------------------------------------------- polling

    def _idle_level(self) -> int:
        return 1 if self._pull == "up" else 0

    def tick(self, now: float) -> None:
        """One poll: read the line, record an edge on change, classify pulses."""
        with self._lock:
            if self._source is None:
                return
            try:
                level = 1 if self._source.read(now) else 0
            except Exception as e:
                log.error("Read failed on GPIO%d: %s — entering NO PIN state.",
                          self._pin, e)
                self._error = f"read failed: {e}"
                try:
                    self._source.close()
                except Exception:
                    pass
                self._source = None
                return

            if self._rate_t0 is None:
                self._rate_t0 = now
            self._rate_reads += 1
            if now - self._rate_t0 >= 1.0:
                self._achieved_hz = self._rate_reads / (now - self._rate_t0)
                self._rate_t0 = now
                self._rate_reads = 0

            idle = self._idle_level()
            if self._level is None:
                self._level = level
                self._nonidle_since = now if level != idle else None
            elif level != self._level:
                self._level = level
                self._seq += 1
                self._edges.append((self._seq, now, level))
                self._counters["edges"] += 1
                if level != idle:
                    self._nonidle_since = now
                elif self._nonidle_since is not None:
                    duration = now - self._nonidle_since
                    kind = "glitches" if duration < self._glitch_s else "presses"
                    self._counters[kind] += 1
                    self._nonidle_since = None

            cutoff = now - self._retention_s
            while self._edges and self._edges[0][1] < cutoff:
                self._edges.popleft()

    def run(self, stop_event: threading.Event) -> None:
        """Thread target: poll at sample_hz (best effort, resync if behind)."""
        interval = 1.0 / self.sample_hz
        next_t = self._time_fn()
        while not stop_event.is_set():
            self.tick(self._time_fn())
            next_t += interval
            delay = next_t - self._time_fn()
            if delay > 0:
                time.sleep(delay)
            else:
                next_t = self._time_fn()  # fell behind — don't try to catch up

    # ------------------------------------------------- live reconfiguration

    def set_input(self, pin: int, pull: str) -> Tuple[bool, str]:
        """Switch the observed line (§5.1). Returns (ok, error message).

        On failure the previous pin is kept (re-claimed if it had to be
        released first); if even that fails the sampler enters NO PIN state.
        """
        if pull not in PULLS:
            return False, f"pull must be one of {PULLS}"
        with self._lock:
            if (pin, pull) == (self._pin, self._pull) and self._source is not None:
                return True, ""
            old = self._source
            if old is not None and pin == self._pin:
                # Same line, different pull: must release before re-claiming.
                try:
                    old.close()
                except Exception:
                    log.exception("Closing GPIO%d failed.", self._pin)
                self._source = None
                try:
                    self._source = self._factory(pin, pull)
                except Exception as e:
                    try:
                        self._source = self._factory(self._pin, self._pull)
                    except Exception as e2:
                        self._error = (f"could not re-claim GPIO{self._pin}: {e2}")
                        log.error("Pin swap failed (%s) and re-claim failed (%s) "
                                  "— NO PIN.", e, e2)
                        return False, (f"{e} — and re-claiming GPIO{self._pin} "
                                       f"failed too: {e2}")
                    log.warning("Pull change on GPIO%d failed (%s) — kept %s.",
                                pin, e, self._pull)
                    return False, str(e)
            else:
                # Different pin: claim the new one first, then release the old —
                # no gap where we hold nothing.
                try:
                    new = self._factory(pin, pull)
                except Exception as e:
                    log.warning("Could not claim GPIO%d (%s) — kept GPIO%d.",
                                pin, e, self._pin)
                    return False, str(e)
                if old is not None:
                    try:
                        old.close()
                    except Exception:
                        log.exception("Closing GPIO%d failed.", self._pin)
                self._source = new
            log.info("Now observing GPIO%d (pull-%s).", pin, pull)
            self._pin, self._pull = pin, pull
            self._error = ""
            self._clear_locked()
            return True, ""

    def _clear_locked(self) -> None:
        """New line → old edges/counters are meaningless. Bumps the epoch so
        connected clients clear their windows too."""
        self._edges.clear()
        self._level = None
        self._nonidle_since = None
        for k in self._counters:
            self._counters[k] = 0
        self._achieved_hz = 0.0
        self._rate_t0 = None
        self._rate_reads = 0
        self._epoch += 1

    def reset_counters(self) -> None:
        with self._lock:
            for k in self._counters:
                self._counters[k] = 0

    # ------------------------------------------------------------ snapshots

    def now(self) -> float:
        return self._time_fn()

    def snapshot_since(self, seq: int) -> dict:
        """Edges newer than seq + current state, atomically (feeds SSE, §6)."""
        with self._lock:
            edges = [e for e in self._edges if e[0] > seq]
            return {
                "now": self._time_fn(),
                "epoch": self._epoch,
                "latest_seq": self._seq,
                "level": self._level,
                "idle": self._idle_level(),
                "active": self._source is not None,
                "pin": self._pin,
                "pull": self._pull,
                "error": self._error,
                "edges": [{"t": t, "v": v} for _, t, v in edges],
                "counters": dict(self._counters,
                                 achieved_hz=round(self._achieved_hz, 1)),
            }

    def status(self) -> dict:
        snap = self.snapshot_since(self._seq_now())
        snap.pop("edges")
        snap["sample_hz"] = self.sample_hz
        snap["glitch_ms"] = self.glitch_ms
        return snap

    def _seq_now(self) -> int:
        with self._lock:
            return self._seq

    def close(self) -> None:
        with self._lock:
            if self._source is not None:
                try:
                    self._source.close()
                finally:
                    self._source = None
