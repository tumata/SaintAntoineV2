"""Volume backends: real (ALSA `amixer`) and mock.

Controls the Pi's **system master** output volume (SPECS_PI_VOLUME §2), not
pygame's per-stream volume. AmixerVolume shells out to `amixer`; every failure
mode (missing binary, timeout, non-zero exit, unparseable output) is logged and
swallowed so a volume problem can never take down playback (V4).

A configurable floor (`volume_min_pct`) keeps a headless Pi from being silenced
from the slider — the deployed goal is to play *louder*, never below the floor.
"""

from __future__ import annotations

import abc
import logging
import re
import subprocess
from typing import Optional

from .config import Config

log = logging.getLogger(__name__)

_AMIXER_TIMEOUT_S = 2.0
# First "[NN%]" token in `amixer sget <control>` output (per-channel volume).
_PCT_RE = re.compile(r"\[(\d{1,3})%\]")
# Control name in `amixer scontrols`: Simple mixer control 'PCM',0
_CONTROL_RE = re.compile(r"Simple mixer control '([^']+)'")


def clamp(pct: int, floor: int) -> int:
    """Clamp a percentage into [floor, 100]."""
    return max(floor, min(100, pct))


class VolumeControl(abc.ABC):
    @abc.abstractmethod
    def get(self) -> Optional[int]:
        """Current master volume 0-100, or None if it can't be read."""

    @abc.abstractmethod
    def set(self, pct: int) -> None:
        """Set master volume; clamps into [floor, 100]."""


class MockVolume(VolumeControl):
    """In-memory volume for mock mode and tests."""

    def __init__(self, initial: int = 50, floor: int = 0):
        self.floor = floor
        self.level = clamp(initial, floor)

    def get(self) -> Optional[int]:
        return self.level

    def set(self, pct: int) -> None:
        self.level = clamp(pct, self.floor)
        log.info("Mock volume set to %d%%.", self.level)


class AmixerVolume(VolumeControl):
    """ALSA master mixer via `amixer` (system-wide, SPECS_PI_VOLUME §2)."""

    def __init__(self, control: str = "Master", card: str = "", floor: int = 0):
        self.control = control
        self.card = card
        self.floor = floor

    def _base_cmd(self) -> list[str]:
        cmd = ["amixer"]
        if self.card:
            cmd += ["-c", self.card]
        return cmd

    def _run(self, *args: str) -> Optional[str]:
        try:
            result = subprocess.run(
                self._base_cmd() + list(args),
                capture_output=True, text=True, timeout=_AMIXER_TIMEOUT_S, check=True,
            )
            return result.stdout
        except FileNotFoundError:
            log.error("amixer not found — volume control unavailable.")
        except subprocess.TimeoutExpired:
            log.error("amixer timed out after %.1f s (args=%s).", _AMIXER_TIMEOUT_S, args)
        except subprocess.CalledProcessError as e:
            log.error("amixer failed (args=%s): %s", args, e.stderr or e)
        return None

    def get(self) -> Optional[int]:
        out = self._run("sget", self.control)
        if out is None:
            return None
        m = _PCT_RE.search(out)
        if not m:
            log.error("Could not parse volume from amixer output for control %r.", self.control)
            return None
        return int(m.group(1))

    def set(self, pct: int) -> None:
        target = clamp(pct, self.floor)
        out = self._run("sset", self.control, f"{target}%")
        if out is None:
            # Surface as a failure so the API/UI can report "unavailable" (§4).
            raise VolumeError(f"amixer sset {self.control} {target}% failed")
        log.info("Volume set to %d%% (control=%r).", target, self.control)

    def list_controls(self) -> list[str]:
        """Available simple-control names from `amixer scontrols` (empty on failure)."""
        out = self._run("scontrols")
        if out is None:
            return []
        return _CONTROL_RE.findall(out)

    def resolve_control(self) -> None:
        """Log available controls and, if the configured one isn't present, fall
        back to the first available so a wrong name degrades gracefully (§3.1)."""
        controls = self.list_controls()
        if not controls:
            return  # amixer unavailable; get()/set() will log their own errors
        log.info("ALSA simple controls available: %s", ", ".join(controls))
        if self.control not in controls:
            log.warning("Mixer control %r not found; falling back to %r.",
                        self.control, controls[0])
            self.control = controls[0]


class VolumeError(Exception):
    pass


def create_volume_control(mode: str, cfg: Config) -> VolumeControl:
    """AmixerVolume in real mode (logs available controls once); MockVolume in
    mock mode so the dev dashboard slider works without ALSA."""
    floor = cfg.volume_min_pct
    if mode == "real":
        vol = AmixerVolume(cfg.mixer_control, cfg.mixer_card, floor=floor)
        vol.resolve_control()
        return vol
    log.info("Mock mode: in-memory volume control.")
    return MockVolume(floor=floor)
