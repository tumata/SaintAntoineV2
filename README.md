# Saint Antoine V2

Push-button → 3 relays ON + a song plays → song ends → relays OFF.
Press again mid-song to switch songs (relays stay on). Runs for real on a
Raspberry Pi 4 and fully mocked on any machine, with an always-on web
dashboard (fake-press, relay status, live logs, restart) reachable over
Tailscale.

Full requirements: [SPECS.md](SPECS.md).

## Quick start (dev machine, mock mode)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .[dev]
pip install pygame-ce          # real local audio (use pygame-ce: plain pygame has no wheels yet for Python 3.13+)
cp config.example.yaml config.yaml   # optional — defaults work out of the box

# Drop a few sample songs into MockMusics/ (mp3/wav/ogg/flac), then:
python -m saintantoine --mock
```

Open http://localhost:8080 — click **Press button** to fake a press, watch the
relay lamps and live logs. If `pygame` is installed locally, mock mode plays
the MockMusics tracks audibly; otherwise playback is simulated (~10 s per track).

The dashboard **Restart** button exits the process (clean: relays OFF). Under
systemd it respawns automatically; locally use the loop runner so it does too:

```bash
./scripts/dev_run.sh
```

## Tests

```bash
pytest
```

All hardware and audio is mocked; no Pi, no sound device, no MockMusics files
needed (tests create their own temp tracks).

## Raspberry Pi deployment

```bash
# On the Pi, as user pi:
cd /home/pi && git clone <this-repo> SaintAntoineV2 && cd SaintAntoineV2
sudo apt install python3-pygame python3-gpiozero python3-flask python3-yaml  # or pip install .[pi]
cp config.example.yaml config.yaml    # adjust if wiring/paths differ

# Music goes in /home/pi/musique (configurable: music_folder)

sudo cp deploy/saintantoine.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now saintantoine
```

Operate:

```bash
systemctl status saintantoine
sudo systemctl restart saintantoine   # also: stop / start
journalctl -u saintantoine -f         # live logs
```

Dashboard over Tailscale: `http://<pi-tailscale-name>:8080`.

Upgrading from V1? The old crontab-based setup and how it was retired are
documented in [deploy/LEGACY_V1.md](deploy/LEGACY_V1.md).

## Wiring (defaults, all in config.yaml)

| Function | GPIO (BCM) | Notes |
|---|---|---|
| Button | 22 | pull-up (`button_pull_up: true`) — button to GND, pressed = LOW |
| Relays | 26, 20, 21 | active-low board, OFF at startup |

Phantom-press hardening is in software (hold-to-trigger sampling, min interval,
rapid-fire lockout) **and** recommended in hardware: ~10 kΩ pull resistor +
~100 nF capacitor across the button to ground, short button wires routed away
from relay/mains wiring. See SPECS §8.

## Runbook notes

- **Audio wedged / no sound after days:** the watchdog re-inits the mixer
  automatically; if that keeps failing it exits non-zero and systemd respawns
  the process. Manual fix: dashboard **Restart** button, or
  `sudo systemctl restart saintantoine`. No Pi reboot needed.
- **Relays after a hard kill:** a clean stop turns relays OFF; even after a
  `SIGKILL`, relays re-initialize OFF on the next start.
- **No tracks found:** the service stays up so the dashboard shows the error;
  fix the folder and restart.
