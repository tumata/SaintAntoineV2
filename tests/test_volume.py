"""Volume backends: amixer parsing/clamping without real ALSA, and MockVolume."""

import subprocess

import pytest

from saintantoine.config import Config
from saintantoine.volume import (
    AmixerVolume,
    MockVolume,
    VolumeError,
    clamp,
    create_volume_control,
)

# Real `amixer sget Master` output from the deployed Pi 4 (default card).
AMIXER_SGET = """Simple mixer control 'Master',0
  Capabilities: pvolume pswitch pswitch-joined
  Playback channels: Front Left - Front Right
  Limits: Playback 0 - 65536
  Mono:
  Front Left: Playback 28820 [44%] [on]
  Front Right: Playback 28820 [44%] [on]
"""


def _fake_run(stdout="", *, raises=None):
    def run(cmd, **kwargs):
        if raises is not None:
            raise raises
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")
    return run


def test_clamp():
    assert clamp(10, floor=50) == 50
    assert clamp(75, floor=50) == 75
    assert clamp(150, floor=50) == 100
    assert clamp(0, floor=0) == 0


def test_mock_volume_clamps_to_floor():
    vol = MockVolume(initial=44, floor=50)
    assert vol.get() == 50           # initial clamped up to floor
    vol.set(80)
    assert vol.get() == 80
    vol.set(10)
    assert vol.get() == 50           # set below floor clamps up


def test_amixer_get_parses_percentage(monkeypatch):
    vol = AmixerVolume("Master")
    monkeypatch.setattr(subprocess, "run", _fake_run(AMIXER_SGET))
    assert vol.get() == 44


def test_amixer_get_returns_none_when_missing(monkeypatch):
    vol = AmixerVolume("Master")
    monkeypatch.setattr(subprocess, "run", _fake_run(raises=FileNotFoundError()))
    assert vol.get() is None


def test_amixer_get_returns_none_on_timeout(monkeypatch):
    vol = AmixerVolume("Master")
    monkeypatch.setattr(subprocess, "run",
                        _fake_run(raises=subprocess.TimeoutExpired("amixer", 2.0)))
    assert vol.get() is None


def test_amixer_get_returns_none_when_unparseable(monkeypatch):
    vol = AmixerVolume("Bogus")
    monkeypatch.setattr(subprocess, "run", _fake_run("no percentage here"))
    assert vol.get() is None


def test_amixer_set_clamps_and_passes_percent(monkeypatch):
    vol = AmixerVolume("Master", floor=50)
    calls = []
    def run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    monkeypatch.setattr(subprocess, "run", run)
    vol.set(10)                      # below floor -> clamped to 50
    assert calls[-1] == ["amixer", "sset", "Master", "50%"]
    vol.set(85)
    assert calls[-1] == ["amixer", "sset", "Master", "85%"]


def test_amixer_set_with_card(monkeypatch):
    vol = AmixerVolume("PCM", card="1", floor=0)
    calls = []
    def run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    monkeypatch.setattr(subprocess, "run", run)
    vol.set(70)
    assert calls[-1] == ["amixer", "-c", "1", "sset", "PCM", "70%"]


def test_amixer_set_raises_on_failure(monkeypatch):
    vol = AmixerVolume("Master")
    monkeypatch.setattr(subprocess, "run",
                        _fake_run(raises=subprocess.CalledProcessError(1, "amixer", stderr="boom")))
    with pytest.raises(VolumeError):
        vol.set(60)


def test_factory_mock_mode():
    cfg = Config()
    vol = create_volume_control("mock", cfg)
    assert isinstance(vol, MockVolume)
    assert vol.floor == cfg.volume_min_pct


def test_factory_real_mode(monkeypatch):
    cfg = Config(mixer_control="Master")
    monkeypatch.setattr(subprocess, "run", _fake_run(AMIXER_SGET))
    vol = create_volume_control("real", cfg)
    assert isinstance(vol, AmixerVolume)
    assert vol.control == "Master"
    assert vol.floor == cfg.volume_min_pct
