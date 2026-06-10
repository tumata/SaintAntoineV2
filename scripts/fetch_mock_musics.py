#!/usr/bin/env python3
"""Fetch real public-domain recordings and crop them to 10-second clips in
MockMusics/.

Source: Internet Archive's George Blood 78 rpm digitization collection,
filtered to John Philip Sousa recordings published before 1926 — both the
compositions (Sousa, d. 1932) and the sound recordings (pre-1926, per the
Music Modernization Act) are in the US public domain.

Requires: pip install soundfile numpy   (decode OGG/MP3/FLAC, write WAV)
Usage:    python3 scripts/fetch_mock_musics.py
"""

import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np
import soundfile as sf

OUT_DIR = Path(__file__).resolve().parent.parent / "MockMusics"
TARGET_COUNT = 10
CLIP_SECONDS = 10.0
START_OFFSET_S = 5.0   # skip 78rpm lead-in noise
FADE_OUT_S = 1.0
FADE_IN_S = 0.05

USER_AGENT = "SaintAntoineV2-mock-music-fetcher/1.0 (dev tooling)"
DOWNLOAD_PAUSE_S = 3
MAX_FILE_MB = 40
# Preferred derivative formats, best first
FORMAT_ORDER = ["Ogg Vorbis", "VBR MP3", "FLAC"]


def http_json(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def find_items(rows: int = 25) -> list:
    """Identifiers of Sousa 78s in the George Blood collection."""
    # date filter: only pre-1926 recordings are US public domain (MMA);
    # the compositions (Sousa, d. 1932) are PD as well.
    query = urllib.parse.urlencode({
        "q": 'collection:georgeblood AND "sousa" AND date:[1890-01-01 TO 1925-12-31]',
        "fl[]": "identifier",
        "rows": rows,
        "output": "json",
        "sort[]": "downloads desc",   # popular items first: well-digitized
    })
    data = http_json(f"https://archive.org/advancedsearch.php?{query}")
    return [d["identifier"] for d in data["response"]["docs"]]


def pick_audio_file(identifier: str):
    """Best downloadable audio file (name, format) for an item, or None."""
    meta = http_json(f"https://archive.org/metadata/{identifier}")
    files = meta.get("files", [])
    for wanted in FORMAT_ORDER:
        for f in files:
            size_ok = int(f.get("size", 0) or 0) < MAX_FILE_MB * 1e6
            if f.get("format") == wanted and size_ok:
                return f["name"]
    return None


def slugify(identifier: str) -> str:
    # "78_the-black-horse-troop_sousas-band-sousa_gbia0363522b" -> "the-black-horse-troop"
    parts = identifier.split("_")
    name = parts[1] if len(parts) > 2 else identifier
    name = re.sub(r"[^A-Za-z0-9-]+", "-", name).strip("-").lower()
    return name[:48] or "track"


def download(url: str, dest: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=180) as response, open(dest, "wb") as fh:
        fh.write(response.read())


def crop_to_clip(src: Path, dest: Path) -> bool:
    """Take CLIP_SECONDS from src (skipping the lead-in), fade edges, write WAV."""
    try:
        audio, rate = sf.read(src, always_2d=True)
    except Exception as e:
        print(f"  cannot decode ({e}); skipping")
        return False
    needed = int(CLIP_SECONDS * rate)
    start = int(START_OFFSET_S * rate)
    if audio.shape[0] < start + needed:
        start = 0
    if audio.shape[0] < needed:
        print("  shorter than 10 s; skipping")
        return False
    clip = audio[start:start + needed].copy()

    fade_in = int(FADE_IN_S * rate)
    fade_out = int(FADE_OUT_S * rate)
    clip[:fade_in] *= np.linspace(0.0, 1.0, fade_in)[:, None]
    clip[-fade_out:] *= np.linspace(1.0, 0.0, fade_out)[:, None]

    peak = np.max(np.abs(clip))
    if peak > 0:
        clip *= 0.9 / max(peak, 0.9)  # normalize down only if clipping

    sf.write(dest, clip, rate, subtype="PCM_16")
    return True


def main() -> int:
    OUT_DIR.mkdir(exist_ok=True)
    identifiers = find_items()
    print(f"Found {len(identifiers)} candidate items on archive.org.", flush=True)
    if not identifiers:
        return 1

    # Resume: keep clips from a previous run
    existing = sorted(OUT_DIR.glob("[0-9][0-9]-*.wav"))
    existing_slugs = {re.sub(r"^\d\d-", "", p.stem) for p in existing}
    made = len(existing)
    if made:
        print(f"Keeping {made} existing clip(s).", flush=True)

    for identifier in identifiers:
        if made >= TARGET_COUNT:
            break
        slug = slugify(identifier)
        if slug in existing_slugs:
            continue
        print(f"[{made + 1}/{TARGET_COUNT}] {identifier}", flush=True)
        tmp = OUT_DIR / ".download.tmp"
        try:
            filename = pick_audio_file(identifier)
            if not filename:
                print("  no suitable audio file; skipping", flush=True)
                continue
            url = f"https://archive.org/download/{identifier}/{urllib.parse.quote(filename)}"
            download(url, tmp)
            dest = OUT_DIR / f"{made + 1:02d}-{slug}.wav"
            if crop_to_clip(tmp, dest):
                made += 1
                existing_slugs.add(slug)
                print(f"  -> {dest.name}", flush=True)
            time.sleep(DOWNLOAD_PAUSE_S)
        except Exception as e:
            print(f"  failed ({e}); skipping", flush=True)
        finally:
            tmp.unlink(missing_ok=True)

    print(f"Done: {made} clip(s) in {OUT_DIR}", flush=True)
    return 0 if made >= TARGET_COUNT else 1


if __name__ == "__main__":
    sys.exit(main())
