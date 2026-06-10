"""Shuffle-bag track selection (D1) and music-folder scanning.

The bag guarantees every track plays once before any repeats, and that the
last track of one cycle is never the first of the next — so the same song can
never play twice in a row (except with a single-track folder, by necessity).
"""

from __future__ import annotations

import logging
import random
from pathlib import Path
from typing import List, Optional, Sequence

log = logging.getLogger(__name__)


def scan_tracks(folder: Path, extensions: Sequence[str]) -> List[str]:
    """Returns sorted absolute paths of playable files in `folder` (non-recursive)."""
    exts = {e.lower() for e in extensions}
    try:
        files = [
            str(p.resolve())
            for p in folder.iterdir()
            if p.is_file() and p.suffix.lower() in exts
        ]
    except OSError as e:
        log.error("Cannot read music folder %s: %s", folder, e)
        return []
    return sorted(files)


class ShuffleBag:
    def __init__(self, tracks: Sequence[str], rng: Optional[random.Random] = None):
        self._rng = rng or random.Random()
        self._tracks: List[str] = list(dict.fromkeys(tracks))
        self._bag: List[str] = []
        self._last: Optional[str] = None

    @property
    def tracks(self) -> List[str]:
        return list(self._tracks)

    def remaining(self) -> int:
        return len(self._bag)

    def discard(self, track: str) -> None:
        """Drop a track (e.g. file vanished from disk) from the bag and the pool."""
        self._tracks = [t for t in self._tracks if t != track]
        self._bag = [t for t in self._bag if t != track]

    def _refill(self) -> None:
        self._bag = list(self._tracks)
        self._rng.shuffle(self._bag)
        # Cycle boundary: the next pick must differ from the previous cycle's last track
        if self._last is not None and len(self._bag) > 1 and self._bag[0] == self._last:
            i = self._rng.randrange(1, len(self._bag))
            self._bag[0], self._bag[i] = self._bag[i], self._bag[0]
        log.info("Shuffle bag refilled with %d track(s).", len(self._bag))

    def next(self, exclude: Optional[str] = None) -> Optional[str]:
        """Next track from the bag, never equal to `exclude` when an alternative
        exists. Returns None when the pool is empty."""
        if not self._tracks:
            return None
        if not self._bag:
            self._refill()
        idx = 0
        if exclude is not None and self._bag[idx] == exclude:
            alt = next((i for i, t in enumerate(self._bag) if t != exclude), None)
            if alt is not None:
                idx = alt
            else:
                log.info("Only one track available; replaying %s", self._bag[idx])
        track = self._bag.pop(idx)
        self._last = track
        return track
