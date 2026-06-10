"""Saint Antoine V2 — button-triggered relays + music player.

Single process: physical button (or web fake-press) starts a song and turns
3 relays on; relays turn off when the song ends. Runs for real on a Raspberry
Pi and fully mocked on a dev machine. See SPECS.md for the full requirements.
"""

__version__ = "2.0.0"
