#!/usr/bin/env python3
"""Generate 10 distinct ~10-second melody WAVs into MockMusics/ for mock-mode
testing. Pure stdlib (wave + math) — no dependencies, fully offline.

Usage: python3 scripts/generate_mock_musics.py
"""

import math
import struct
import wave
from pathlib import Path

SAMPLE_RATE = 44100
DURATION_S = 10.0
AMPLITUDE = 0.4

OUT_DIR = Path(__file__).resolve().parent.parent / "MockMusics"

# Note frequencies (Hz)
NOTES = {
    "C4": 261.63, "D4": 293.66, "E4": 329.63, "F4": 349.23, "G4": 392.00,
    "A4": 440.00, "B4": 493.88, "C5": 523.25, "D5": 587.33, "E5": 659.25,
    "F5": 698.46, "G5": 783.99, "A5": 880.00,
    "A3": 220.00, "B3": 246.94, "G3": 196.00, "E3": 164.81, "F3": 174.61,
}

# Each song: (filename, tempo in notes/sec, waveform, melody loop)
SONGS = [
    ("01-ascension",  2.0, "sine",     ["C4", "E4", "G4", "C5", "G4", "E4"]),
    ("02-clocher",    1.6, "triangle", ["G4", "C5", "E5", "C5", "G4", "C4"]),
    ("03-guinguette", 2.5, "square",   ["C4", "C4", "G4", "G4", "A4", "A4", "G4", "F4", "E4", "D4"]),
    ("04-reverie",    1.2, "sine",     ["A3", "C4", "E4", "A4", "E4", "C4"]),
    ("05-fanfare",    2.2, "square",   ["C4", "F4", "A4", "C5", "A4", "F4", "C5", "C5"]),
    ("06-ruisseau",   3.0, "sine",     ["E4", "G4", "B4", "E5", "D5", "B4", "G4", "F4"]),
    ("07-carillon",   1.8, "triangle", ["C5", "A4", "F4", "A4", "G4", "E4", "C4", "E4"]),
    ("08-procession", 1.0, "sine",     ["E3", "G3", "B3", "E4", "B3", "G3"]),
    ("09-farandole",  3.2, "square",   ["D4", "E4", "F4", "G4", "A4", "G4", "F4", "E4"]),
    ("10-vepres",     1.4, "triangle", ["F4", "A4", "C5", "F5", "E5", "C5", "A4", "G4"]),
]


def sample(waveform: str, phase: float) -> float:
    if waveform == "square":
        return 0.35 if math.sin(phase) >= 0 else -0.35  # quieter: squares are loud
    if waveform == "triangle":
        return 2.0 / math.pi * math.asin(math.sin(phase))
    return math.sin(phase)


def render(tempo: float, waveform: str, melody: list) -> bytes:
    note_len = 1.0 / tempo
    total = int(SAMPLE_RATE * DURATION_S)
    frames = bytearray()
    for i in range(total):
        t = i / SAMPLE_RATE
        idx = int(t / note_len)
        freq = NOTES[melody[idx % len(melody)]]
        t_in_note = t - idx * note_len
        # Per-note attack/release envelope to avoid clicks
        env = min(1.0, t_in_note / 0.02, max(0.0, (note_len - t_in_note) / 0.05))
        # Global fade-out over the last second
        env *= min(1.0, (DURATION_S - t) / 1.0)
        value = AMPLITUDE * env * sample(waveform, 2 * math.pi * freq * t)
        frames += struct.pack("<h", int(value * 32767))
    return bytes(frames)


def main() -> None:
    OUT_DIR.mkdir(exist_ok=True)
    for name, tempo, waveform, melody in SONGS:
        path = OUT_DIR / f"{name}.wav"
        with wave.open(str(path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(SAMPLE_RATE)
            wav.writeframes(render(tempo, waveform, melody))
        print(f"wrote {path.name}")


if __name__ == "__main__":
    main()
