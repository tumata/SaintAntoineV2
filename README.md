# Saint Antoine V2

Push-button → 3 relays ON + a song plays → song ends → relays OFF.
Press again mid-song to switch songs (relays stay on). Runs for real on a
Raspberry Pi 4 and fully mocked on any machine, with an always-on web
dashboard reachable over Tailscale. The landing page shows **live statistics**
(listening hours, favorite songs, busiest days); the operational controls
(fake-press, relay status, live logs, restart) live on a **Debug** page.
The interface is in **French**.

Full requirements: [SPECS.md](SPECS.md).

## Quick start (dev machine, mock mode)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .[dev]
pip install pygame-ce          # real local audio (use pygame-ce: plain pygame has no wheels yet for Python 3.13+)
brew install ffmpeg            # song upload trimming + loudness normalization (SPECS §11.2)
cp config.example.yaml config.yaml   # optional — defaults work out of the box

# Drop a few sample songs into MockMusics/ (mp3/wav/ogg/flac), then:
python -m saintantoine --mock
```

Open http://localhost:8080 — the **Statistiques** landing page graphs play
activity and updates live as songs play. Go to **Debug** and click **Appuyer**
to fake a press, watch the relay lamps and live logs. If `pygame` is installed
locally, mock mode plays the MockMusics tracks audibly; otherwise playback is
simulated (~10 s per track).

Play history is kept in a small SQLite database (`analytics_db_path`; mock mode
uses `./analytics.db`), written from the controller and read by the dashboard.

On the **Songs** page, uploading opens a waveform selector: pick the 10-second
section to keep, preview it, upload — the server (ffmpeg) trims it and
normalizes loudness to -14 LUFS so every song plays at the same volume. A
**Normalize** button on each existing song fixes quiet files in place, no
restart needed (SPECS §11.2).

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
mkdir -p /home/pi/github && cd /home/pi/github && git clone <this-repo> SaintAntoineV2 && cd SaintAntoineV2
sudo apt install python3-pygame python3-gpiozero python3-flask python3-yaml ffmpeg  # or pip install .[pi] (ffmpeg still via apt)
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

### YouTube import (optional)

The Songs page can import audio straight from a YouTube link
([SPECS_YOUTUBE_IMPORT.md](SPECS_YOUTUBE_IMPORT.md)). It needs `yt-dlp` (the import
field is hidden when it's absent), which YouTube breaks periodically — so keep it
current with a daily cron:

```bash
sudo pip install --break-system-packages "yt-dlp[default]"   # [default] bundles the JS challenge-solver (yt-dlp-ejs)
crontab -e                                                   # as user pi, add:
# 0 4 * * *  /home/pi/github/SaintAntoineV2/scripts/update-ytdlp.sh >> /home/pi/github/SaintAntoineV2/ytdlp-update.log 2>&1
```

For robust extraction (YouTube's "n challenge"), also install a JS runtime —
on a 64-bit OS (`uname -m` → `aarch64`): `curl -fsSL https://deno.land/install.sh | sh`
then `sudo ln -sf ~/.deno/bin/deno /usr/local/bin/deno` so the systemd service finds it.

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
