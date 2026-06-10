"""Audio backends: real (pygame.mixer) and mock.

MockAudio simulates playback on an injectable clock and can fake the failure
modes the watchdog must handle (play raises, playback never starts, health
probe fails, re-init keeps failing).
"""

from __future__ import annotations

import abc
import logging
import time
from typing import Callable, Dict, Optional

from .config import Config

log = logging.getLogger(__name__)


class AudioError(Exception):
    pass


class AudioBackend(abc.ABC):
    @abc.abstractmethod
    def init(self) -> None: ...

    @abc.abstractmethod
    def load_and_play(self, path: str) -> None: ...

    @abc.abstractmethod
    def stop(self) -> None: ...

    @abc.abstractmethod
    def is_busy(self) -> bool: ...

    @abc.abstractmethod
    def health_check(self) -> bool: ...

    @abc.abstractmethod
    def reinit(self) -> None: ...

    @abc.abstractmethod
    def quit(self) -> None: ...


class MockAudio(AudioBackend):
    def __init__(self, clock: Callable[[], float] = time.monotonic, default_duration: float = 10.0):
        self.clock = clock
        self.default_duration = default_duration
        self.durations: Dict[str, float] = {}
        self.current_path: Optional[str] = None
        self._started_at: Optional[float] = None
        self.initialized = False
        self.reinit_count = 0
        # Failure injection for watchdog tests
        self.fail_on_play = False
        self.silent = False          # play "succeeds" but is_busy never goes true
        self.healthy = True
        self.fail_reinit = False
        self.reinit_fixes = True     # a successful reinit clears injected faults

    def init(self) -> None:
        self.initialized = True
        log.info("Mock audio initialized.")

    def load_and_play(self, path: str) -> None:
        if not self.initialized:
            raise AudioError("mock mixer not initialized")
        if self.fail_on_play:
            raise AudioError("mock failure: load/play raised")
        self.current_path = path
        self._started_at = self.clock()
        log.info("Mock audio playing %s (simulated %.1f s).",
                 path, self.durations.get(path, self.default_duration))

    def stop(self) -> None:
        self.current_path = None
        self._started_at = None

    def is_busy(self) -> bool:
        if self.silent or self.current_path is None or self._started_at is None:
            return False
        duration = self.durations.get(self.current_path, self.default_duration)
        if self.clock() - self._started_at >= duration:
            self.current_path = None
            self._started_at = None
            return False
        return True

    def health_check(self) -> bool:
        return self.initialized and self.healthy

    def reinit(self) -> None:
        self.reinit_count += 1
        if self.fail_reinit:
            raise AudioError("mock failure: reinit raised")
        self.quit()
        self.init()
        if self.reinit_fixes:
            self.healthy = True
            self.silent = False
            self.fail_on_play = False

    def quit(self) -> None:
        self.stop()
        self.initialized = False


class RealAudio(AudioBackend):
    """pygame.mixer wrapper (imported lazily). System default output device (D8)."""

    def __init__(self):
        import pygame  # imported here: optional dependency

        self._pygame = pygame
        self._initialized = False

    def init(self) -> None:
        try:
            self._pygame.mixer.init()
            self._initialized = True
            log.info("pygame mixer initialized: %s", self._pygame.mixer.get_init())
        except Exception as e:
            self._initialized = False
            raise AudioError(f"mixer init failed: {e}") from e

    def load_and_play(self, path: str) -> None:
        try:
            self._pygame.mixer.music.load(path)
            self._pygame.mixer.music.play()
        except Exception as e:
            raise AudioError(f"load/play failed for {path}: {e}") from e

    def stop(self) -> None:
        try:
            if self._pygame.mixer.get_init():
                self._pygame.mixer.music.stop()
        except Exception as e:
            raise AudioError(f"stop failed: {e}") from e

    def is_busy(self) -> bool:
        try:
            if not self._pygame.mixer.get_init():
                return False
            return bool(self._pygame.mixer.music.get_busy())
        except Exception as e:
            raise AudioError(f"busy check failed: {e}") from e

    def health_check(self) -> bool:
        try:
            return self._pygame.mixer.get_init() is not None
        except Exception:
            return False

    def reinit(self) -> None:
        log.warning("Re-initializing pygame mixer.")
        try:
            self._pygame.mixer.quit()
        except Exception:
            pass
        self.init()

    def quit(self) -> None:
        try:
            self._pygame.mixer.quit()
        except Exception:
            pass
        self._initialized = False


def create_audio_backend(mode: str, cfg: Config) -> AudioBackend:
    """Real mode requires pygame. Mock mode prefers real local audio when pygame
    is available (so MockMusics tracks are audibly played on a dev machine),
    falling back to simulated playback otherwise; force_mock_audio overrides."""
    if mode == "real":
        return RealAudio()
    if cfg.force_mock_audio:
        log.info("Mock mode: simulated audio (force_mock_audio).")
        return MockAudio()
    try:
        backend = RealAudio()
        log.info("Mock mode: pygame available — playing real audio locally.")
        return backend
    except ImportError:
        log.info("Mock mode: pygame not installed — simulated audio.")
        return MockAudio()
