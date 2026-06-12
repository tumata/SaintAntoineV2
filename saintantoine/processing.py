"""Audio processing via ffmpeg (SPECS §11.2): clip extraction and EBU R128
loudness normalization.

Uploads are trimmed to a fixed-length clip and normalized; existing library
files can be normalized in place. ffmpeg/ffprobe are optional system
dependencies — callers must check ffmpeg_available() and degrade gracefully.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import List, Optional

log = logging.getLogger(__name__)

SUBPROCESS_TIMEOUT_S = 120
FADE_S = 0.3
TRUE_PEAK_DB = -1.5
LOUDNESS_RANGE = 11

# Output container/encoder per suffix. Unlisted suffixes fall back to the bare
# extension as the format name and ffmpeg's default encoder for it.
FORMATS = {".mp3": "mp3", ".wav": "wav", ".ogg": "ogg", ".flac": "flac"}
ENCODERS = {".mp3": ["-codec:a", "libmp3lame", "-q:a", "2"]}


class ProcessingError(Exception):
    pass


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _run(cmd: List[str], what: str) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=SUBPROCESS_TIMEOUT_S)
    except FileNotFoundError as e:
        raise ProcessingError(f"{what}: {cmd[0]} is not installed") from e
    except subprocess.TimeoutExpired as e:
        raise ProcessingError(f"{what}: timed out after {SUBPROCESS_TIMEOUT_S} s") from e
    if proc.returncode != 0:
        log.error("%s failed (%s): %s", cmd[0], what, proc.stderr.strip()[-2000:])
        raise ProcessingError(f"{what}: {cmd[0]} exited with code {proc.returncode}")
    return proc


def probe_duration(path: Path) -> float:
    proc = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
                f"probe {path.name}")
    try:
        return float(proc.stdout.strip())
    except ValueError as e:
        raise ProcessingError(f"probe {path.name}: unreadable duration "
                              f"{proc.stdout.strip()!r}") from e


def _loudnorm(target_lufs: float) -> str:
    return f"loudnorm=I={target_lufs:g}:TP={TRUE_PEAK_DB:g}:LRA={LOUDNESS_RANGE:g}"


def _trim_args(start_s: Optional[float], duration_s: Optional[float]) -> List[str]:
    args: List[str] = []
    if start_s is not None:
        args += ["-ss", f"{start_s:.3f}"]
    if duration_s is not None:
        args += ["-t", f"{duration_s:.3f}"]
    return args


def measure_loudness(path: Path, target_lufs: float,
                     start_s: Optional[float] = None,
                     duration_s: Optional[float] = None) -> dict:
    """Pass 1 of two-pass loudnorm: measured stats for the (sub)file as a dict.

    The same -ss/-t trim must be used again in pass 2, or the measured values
    won't describe the audio they are applied to.
    """
    cmd = (["ffmpeg", "-hide_banner", "-nostdin"]
           + _trim_args(start_s, duration_s)
           + ["-i", str(path), "-vn",
              "-af", _loudnorm(target_lufs) + ":print_format=json",
              "-f", "null", "-"])
    proc = _run(cmd, f"measure {path.name}")
    return _parse_loudnorm_json(proc.stderr, path.name)


def _parse_loudnorm_json(stderr: str, name: str) -> dict:
    """The loudnorm filter prints its JSON stats as the last block on stderr."""
    start, end = stderr.rfind("{"), stderr.rfind("}")
    if start == -1 or end <= start:
        raise ProcessingError(f"measure {name}: no loudnorm stats in ffmpeg output")
    try:
        return json.loads(stderr[start:end + 1])
    except json.JSONDecodeError as e:
        raise ProcessingError(f"measure {name}: unreadable loudnorm stats") from e


def _measured(stats: dict) -> str:
    try:
        return (f"measured_I={stats['input_i']}:measured_TP={stats['input_tp']}:"
                f"measured_LRA={stats['input_lra']}:measured_thresh={stats['input_thresh']}:"
                f"offset={stats['target_offset']}")
    except KeyError as e:
        raise ProcessingError(f"loudnorm stats missing field {e}") from None


def _encode_atomic(src: Path, dest: Path, trim: List[str], af: str, what: str) -> None:
    """Run one ffmpeg encode into a unique .part temp next to dest, then rename.

    The .part suffix keeps half-written files invisible to the startup scan
    (§11.1), so the container format must be passed explicitly via -f.
    """
    suffix = dest.suffix.lower()
    fd, tmp_name = tempfile.mkstemp(prefix=dest.name + ".", suffix=".part",
                                    dir=str(dest.parent))
    os.close(fd)
    tmp = Path(tmp_name)
    cmd = (["ffmpeg", "-hide_banner", "-nostdin", "-y"]
           + trim + ["-i", str(src), "-vn", "-af", af]
           + ENCODERS.get(suffix, [])
           + ["-f", FORMATS.get(suffix, suffix.lstrip(".")), str(tmp)])
    try:
        _run(cmd, what)
        tmp.replace(dest)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def process_clip(src: Path, dest: Path, start_s: float, clip_s: float,
                 target_lufs: float) -> None:
    """Cut a clip_s-second clip of src at start_s, loudness-normalize it and
    write it to dest (atomically; dest's suffix picks container/encoder).

    start_s is clamped so the window fits; sources shorter than clip_s are
    kept whole. Short fades avoid abrupt cut edges.
    """
    total = probe_duration(src)
    duration = min(clip_s, total)
    start = min(max(0.0, start_s), max(0.0, total - duration))
    stats = measure_loudness(src, target_lufs, start_s=start, duration_s=duration)
    af = _loudnorm(target_lufs) + ":" + _measured(stats) + ":linear=true"
    if duration > 2 * FADE_S:
        af += (f",afade=t=in:d={FADE_S:g}"
               f",afade=t=out:st={duration - FADE_S:.3f}:d={FADE_S:g}")
    _encode_atomic(src, dest, _trim_args(start, duration), af,
                   f"clip {src.name} [{start:.1f}s +{duration:.1f}s]")


def normalize_in_place(path: Path, target_lufs: float) -> None:
    """Two-pass loudnorm of the whole file, atomically replacing the original."""
    stats = measure_loudness(path, target_lufs)
    af = _loudnorm(target_lufs) + ":" + _measured(stats) + ":linear=true"
    _encode_atomic(path, path, [], af, f"normalize {path.name}")
