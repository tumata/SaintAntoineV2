import random

import pytest

from saintantoine.audio import MockAudio
from saintantoine.config import Config
from saintantoine.controller import Controller
from saintantoine.gpio import MockGpio
from saintantoine.selection import ShuffleBag
from saintantoine.webhook import Webhook


class FakeClock:
    """Virtual time: sleep() advances the clock instead of waiting."""

    def __init__(self, start: float = 0.0):
        self.t = start

    def clock(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def fake_clock():
    return FakeClock()


class Harness:
    """A fully mocked controller over virtual time, with real temp track files."""

    def __init__(self, tmp_path, n_tracks=3, track_duration=10.0, cfg=None, seed=42,
                 analytics=None):
        self.clock = FakeClock()
        self.music_folder = tmp_path
        self.tracks = []
        for i in range(n_tracks):
            p = tmp_path / f"song{i}.mp3"
            p.write_bytes(b"\x00")
            self.tracks.append(str(p))
        self.cfg = cfg or Config()
        self.gpio = MockGpio()
        self.audio = MockAudio(clock=self.clock.clock, default_duration=track_duration)
        self.audio.init()
        self.bag = ShuffleBag(self.tracks, rng=random.Random(seed))
        self.shutdown_calls = []
        self.controller = Controller(
            self.gpio,
            self.audio,
            self.bag,
            Webhook(""),  # disabled
            self.cfg,
            mode="mock",
            request_shutdown=self.shutdown_calls.append,
            clock=self.clock.clock,
            analytics=analytics,
        )

    def press(self):
        self.controller.on_press()

    def tick(self):
        self.controller.tick()

    def run_until(self, t):
        """Advance virtual time to t, ticking the supervisor along the way."""
        while self.clock.t < t:
            self.clock.advance(0.1)
            self.tick()


@pytest.fixture
def harness(tmp_path):
    return Harness(tmp_path)


@pytest.fixture
def make_harness(tmp_path):
    def factory(**kwargs):
        return Harness(tmp_path, **kwargs)

    return factory
