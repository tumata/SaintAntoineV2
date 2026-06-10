"""State machine (SPECS §5), playback supervisor and audio watchdog (§9).

Invariants:
- Relays are a pure output: ON iff state is PLAYING, switched only here.
- All transitions are serialized by one re-entrant lock; on_press() (physical
  button or web fake-press) and the supervisor tick cannot race.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Callable, Optional

from .audio import AudioBackend, AudioError
from .config import Config
from .gpio import GpioBackend
from .selection import ShuffleBag
from .webhook import Webhook

log = logging.getLogger(__name__)

IDLE = "IDLE"
PLAYING = "PLAYING"


class Controller:
    def __init__(
        self,
        gpio: GpioBackend,
        audio: AudioBackend,
        bag: ShuffleBag,
        webhook: Webhook,
        cfg: Config,
        mode: str,
        request_shutdown: Optional[Callable[[int], None]] = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.gpio = gpio
        self.audio = audio
        self.bag = bag
        self.webhook = webhook
        self.cfg = cfg
        self.mode = mode
        self.request_shutdown = request_shutdown or (lambda code: None)
        self.clock = clock

        self._lock = threading.RLock()
        self.state = IDLE
        self.current_track: Optional[str] = None
        self.audio_healthy = True
        self._play_started_at: Optional[float] = None
        self._went_busy = False
        self._last_probe_at = clock()
        self._started_at = clock()

    # -- events ---------------------------------------------------------------

    def on_press(self) -> None:
        """Accepted press (already debounced): start a song, or switch songs."""
        with self._lock:
            if self.state == PLAYING:
                log.info("Press while playing: switching track (relays stay ON).")
                try:
                    self.audio.stop()
                except AudioError as e:
                    log.error("Stop failed: %s", e)
                self._start_playback(exclude=self.current_track)
            else:
                log.info("Press while idle: starting playback.")
                self._start_playback(exclude=None)

    def on_track_finished(self) -> None:
        with self._lock:
            if self.state != PLAYING:
                return
            log.info("Track finished naturally: %s", _name(self.current_track))
            self.current_track = None
            self.state = IDLE
            self.gpio.relays_off()

    def shutdown(self) -> None:
        """Clean shutdown path (SIGTERM / /restart): stop playback, relays OFF."""
        with self._lock:
            log.info("Controller shutdown: stopping playback, relays OFF.")
            try:
                self.audio.stop()
            except AudioError as e:
                log.error("Stop during shutdown failed: %s", e)
            self.current_track = None
            self.state = IDLE
            self.gpio.relays_off()

    # -- playback -------------------------------------------------------------

    def _start_playback(self, exclude: Optional[str]) -> bool:
        track = self._select_existing(exclude)
        if track is None:
            log.error("No playable track available; staying IDLE, relays OFF.")
            self.current_track = None
            self.state = IDLE
            self.gpio.relays_off()
            return False

        self.gpio.relays_on()
        try:
            self.audio.load_and_play(track)
        except AudioError as e:
            log.error("Playback start failed for %s: %s", _name(track), e)
            self._handle_audio_fault(f"load/play raised: {e}")
            return False

        self.current_track = track
        self.state = PLAYING
        self._play_started_at = self.clock()
        self._went_busy = False
        log.info("Playing: %s (%d track(s) left in bag).", _name(track), self.bag.remaining())
        self.webhook.fire(_name(track))
        return True

    def _select_existing(self, exclude: Optional[str]) -> Optional[str]:
        """Shuffle-bag pick + pre-play existence check (SPECS §7.3)."""
        while True:
            track = self.bag.next(exclude=exclude)
            if track is None:
                return None
            if os.path.isfile(track):
                return track
            log.warning("Track missing on disk, dropping from pool: %s", _name(track))
            self.bag.discard(track)

    # -- supervisor / watchdog --------------------------------------------------

    def run_supervisor(self, stop_event: threading.Event) -> None:
        log.info("Playback supervisor started.")
        while not stop_event.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("Error in supervisor tick")
            stop_event.wait(self.cfg.supervisor_poll_s)
        log.info("Playback supervisor stopped.")

    def tick(self) -> None:
        """One supervisor pass: natural-end detection + watchdog checks."""
        with self._lock:
            now = self.clock()

            if self.state == PLAYING:
                try:
                    busy = self.audio.is_busy()
                except AudioError as e:
                    self._handle_audio_fault(f"busy check raised: {e}")
                    return
                if busy:
                    self._went_busy = True
                elif self._went_busy:
                    self.on_track_finished()
                elif self._play_started_at is not None and (
                    now - self._play_started_at > self.cfg.play_start_grace_s
                ):
                    self._handle_audio_fault(
                        f"playback never started within {self.cfg.play_start_grace_s:.1f} s grace"
                    )
                return

            # IDLE: periodic lightweight health probe
            if now - self._last_probe_at >= self.cfg.health_probe_interval_s:
                self._last_probe_at = now
                if not self.audio.health_check():
                    self._handle_audio_fault("health probe failed")

    def _handle_audio_fault(self, reason: str) -> None:
        """Watchdog recovery (SPECS §9.2): re-init mixer; safe-IDLE; escalate."""
        log.error("AUDIO FAULT: %s", reason)
        recovered = False
        for attempt in range(1, self.cfg.max_reinit_attempts + 1):
            try:
                self.audio.reinit()
                if self.audio.health_check():
                    log.warning("Audio recovered after re-init attempt %d.", attempt)
                    recovered = True
                    break
                log.error("Re-init attempt %d: health check still failing.", attempt)
            except AudioError as e:
                log.error("Re-init attempt %d raised: %s", attempt, e)

        was_playing = self.state == PLAYING
        faulted_track = self.current_track
        self.current_track = None
        self.state = IDLE
        self.gpio.relays_off()

        if not recovered:
            self.audio_healthy = False
            log.critical("Audio NOT recovered after %d attempt(s).", self.cfg.max_reinit_attempts)
            if self.cfg.escalate_exit:
                log.critical("Escalating: requesting process exit so systemd respawns us.")
                self.request_shutdown(1)
            return

        self.audio_healthy = True
        if was_playing and self.cfg.on_audio_fault == "resume" and faulted_track:
            log.info("Fault policy 'resume': replaying %s.", _name(faulted_track))
            try:
                self.gpio.relays_on()
                self.audio.load_and_play(faulted_track)
                self.current_track = faulted_track
                self.state = PLAYING
                self._play_started_at = self.clock()
                self._went_busy = False
            except AudioError as e:
                log.error("Resume after fault failed (%s); safe-IDLE.", e)
                self.current_track = None
                self.state = IDLE
                self.gpio.relays_off()
        else:
            log.info("Fault policy 'idle': safe state (relays OFF), waiting for next press.")

    # -- status -----------------------------------------------------------------

    def status(self) -> dict:
        with self._lock:
            return {
                "state": self.state,
                "relays": self.gpio.relay_states(),
                "current_track": _name(self.current_track) if self.current_track else None,
                "mode": self.mode,
                "audio_healthy": self.audio_healthy,
                "tracks_total": len(self.bag.tracks),
                "bag_remaining": self.bag.remaining(),
                "uptime_s": round(self.clock() - self._started_at, 1),
            }


def _name(path: Optional[str]) -> str:
    return os.path.basename(path) if path else ""
