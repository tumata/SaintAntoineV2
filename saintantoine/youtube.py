"""YouTube audio import via yt-dlp (SPECS_YOUTUBE_IMPORT).

Paste a YouTube link -> yt-dlp downloads the audio as MP3 on the Pi -> the
existing dashboard trim UI clips it (no new trim path). yt-dlp is an optional
system tool, invoked as a subprocess (never imported), so a missing tool
degrades gracefully and can't break startup.

The download is heavy and shares the Pi with the button/playback loop, so the
work is isolated and hardened (§7.1): a host allowlist before any subprocess, a
hard timeout with a process-group kill (yt-dlp spawns ffmpeg), low scheduling
priority, and a single-flight lock so peak CPU/disk is bounded to one download.
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import threading
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.parse import urlparse

log = logging.getLogger(__name__)

# Only these hosts are ever handed to yt-dlp — everything else is rejected
# before a subprocess runs, limiting the SSRF/abuse surface (§7).
_ALLOWED_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com",
    "music.youtube.com", "youtu.be",
}

# Single-flight: one import at a time bounds peak CPU/disk on the Pi (§7.1).
# The endpoint acquires this non-blocking and returns 429 if it's held.
IMPORT_LOCK = threading.Lock()

_GROUP_KILL_GRACE_S = 2.0


class YoutubeError(Exception):
    pass


def youtube_available() -> bool:
    return shutil.which("yt-dlp") is not None


def is_youtube_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except (ValueError, AttributeError):
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    return (parsed.hostname or "").lower() in _ALLOWED_HOSTS


def parse_filesize(value) -> int:
    """Convert a yt-dlp-style size ("50M", "500K") to bytes for the disk check."""
    text = str(value).strip().upper()
    mult = 1
    if text.endswith("K"):
        mult, text = 1024, text[:-1]
    elif text.endswith("M"):
        mult, text = 1024 ** 2, text[:-1]
    elif text.endswith("G"):
        mult, text = 1024 ** 3, text[:-1]
    try:
        return int(float(text) * mult)
    except ValueError:
        return 50 * 1024 ** 2  # conservative fallback


def _priority_prefix() -> List[str]:
    """Run the download niced/ionice'd so it yields CPU/IO to playback, the
    button thread, and the audio watchdog probe (§7.1). Both tools are optional;
    absent ones are simply skipped."""
    prefix: List[str] = []
    if shutil.which("nice"):
        prefix += ["nice", "-n", "10"]
    if shutil.which("ionice"):
        prefix += ["ionice", "-c3"]
    return prefix


def _kill_group(proc: subprocess.Popen) -> None:
    """SIGTERM then SIGKILL the child's whole process group, so the ffmpeg
    grandchild yt-dlp spawns can't be orphaned and keep eating CPU/disk."""
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, OSError):
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, OSError):
            return
        try:
            proc.wait(timeout=_GROUP_KILL_GRACE_S)
            return
        except subprocess.TimeoutExpired:
            continue


def _output_path(stdout: str) -> Optional[Path]:
    """yt-dlp's `--print after_move:filepath` writes the final file path as the
    last stdout line. A match-filter rejection (too long) prints nothing."""
    for line in reversed((stdout or "").splitlines()):
        line = line.strip()
        if line:
            return Path(line)
    return None


def download_audio(url: str, dest_dir: Path, *, max_duration_s: int,
                   max_filesize: str, timeout_s: float) -> Tuple[Path, str]:
    """Download the audio at `url` as an MP3 into dest_dir; return (path, title).

    Raises YoutubeError on any failure (bad video, too long/large, timeout,
    missing tool). The caller owns dest_dir and its cleanup.
    """
    out_template = str(Path(dest_dir) / "%(title)s.%(ext)s")
    cmd = _priority_prefix() + [
        "yt-dlp",
        "--no-playlist",
        "--no-progress",
        "--no-simulate",
        "--extract-audio", "--audio-format", "mp3", "--audio-quality", "2",
        "--max-filesize", str(max_filesize),
        "--match-filter", f"duration < {int(max_duration_s)}",
        "--print", "after_move:filepath",
        "--output", out_template,
        "--", url,
    ]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, start_new_session=True)
    except FileNotFoundError as e:
        raise YoutubeError("yt-dlp is not installed") from e
    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        proc.communicate()  # reap the killed group
        raise YoutubeError(f"download timed out after {timeout_s:g} s")
    if proc.returncode != 0:
        log.error("yt-dlp failed for %s: %s", url, (stderr or "").strip()[-2000:])
        raise YoutubeError(f"yt-dlp exited with code {proc.returncode}")
    path = _output_path(stdout)
    if path is None or not path.exists():
        # Returncode 0 with no file = match-filter skip (too long) or no audio.
        log.error("yt-dlp produced no file for %s: %s", url, (stderr or "").strip()[-2000:])
        raise YoutubeError("no audio produced (video too long, unavailable, or filtered)")
    return path, path.stem
