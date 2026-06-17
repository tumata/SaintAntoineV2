# Saint Antoine V2 — Button → Relays + Music Controller

## Specification & Requirements Document

> Status: **Draft for review** · Last updated: 2026-06-10
>
> This document captures the full requirements for a Raspberry Pi script that, on a
> physical push-button press, triggers 3 relays and plays a song; when the song ends
> the relays turn off. Pressing again mid-song switches to a new song (relays stay on).
> The script must also run/test on a non-Pi machine via mocked hardware and expose a
> local web dashboard (reachable over Tailscale).
>
> A previous, simpler implementation exists at
> `toilette-st-antoine/toilette.py`. It is a **subset** of this spec and is used here
> only as context — not as a template.

---

## 1. Goals

- **G1** — On a physical button press, turn on 3 relays together and start playing a song.
- **G2** — When a song finishes naturally, turn all relays off.
- **G3** — Pressing the button while a song is playing switches to a *different* song; relays stay on until the new song ends.
- **G4** — Song selection is semi-random: never the same song twice in a row, and every song gets a fair chance to play (shuffle-bag).
- **G5** — Be robust against **phantom / ghost button presses** (the previous build's main pain point).
- **G6** — Run unmocked on a Raspberry Pi *and* mocked on a dev laptop, so the logic is unit-testable and locally verifiable.
- **G7** — Provide an always-on local **web dashboard**: fake-press button, live relay status (color), and a live log view. Reachable from a laptop over Tailscale.
- **G8** — Run as a boot service that can be killed/restarted without rebooting the Pi.
- **G10** — The dashboard provides a **Restart** button to recover the music+relay logic (e.g. after an audio wedge) without SSHing in or rebooting the Pi. Single process (see §6.2).
- **G9** — Survive long uptimes: detect and recover from the audio subsystem failing after hours/days.

---

## 2. Confirmed decisions (from requirements gathering)

| # | Topic | Decision |
|---|-------|----------|
| D1 | **Music selection** | **Shuffle bag**: shuffle all tracks into a queue, play through the entire queue, then reshuffle. Guarantee the last track of one cycle is not the first of the next. Every track plays once before any repeats. |
| D2 | **Web UI role** | **Always on, full dashboard** — runs on both Pi and dev machine. On the Pi it drives real hardware; the web fake-press works *alongside* the physical button. The landing page (`/`) is the **Analytics** page (§11.3); the operational controls (fake-press, relays, live log, restart) move to a **Debug** page (`/debug`, §11) navigated to like Songs. |
| D3 | **Debounce strategy** | **Hold-to-trigger** (sampled/integrating debounce) + `bounce_time` + minimum inter-press interval + rapid-fire lockout. Software complemented by a **hardware RC filter** recommendation (see §8). |
| D4 | **Hardware** | **Reuse current wiring** as defaults, all overridable via config: button on **GPIO22** (`pull_up=True` — button to GND, pressed = LOW; **revised 2026-06-12** after a ButtonScope measurement, see §4), 3 relays on **GPIO26 / GPIO20 / GPIO21**, **active-low** (`active_high=False`). |
| D5 | **Webhook** | **Keep, but optional + generic**: a configurable webhook URL, fire-and-forget, disabled when no URL is set. Sends `{track, timestamp}` (Airtable-compatible). |
| D6 | **Boot** | **systemd service + configurable startup delay**, with clean restart/kill via `systemctl` (no reboot needed). |
| D7 | **Audio formats** | Support **multiple formats** (`.mp3`, `.wav`, `.ogg`, `.flac`). |
| D8 | **Audio output** | **System default** output device (no forced 3.5mm jack). |
| D9 | **Volume / fade** | **No** fade in/out — abrupt start/stop is fine. A dashboard master-volume slider was later added (revising the original "no volume control"), specced and implemented per [SPECS_PI_VOLUME.md](SPECS_PI_VOLUME.md). |
| D10 | **Audio watchdog** | Required — detect the audio subsystem failing on long uptimes and recover automatically (see §9). |
| D11 | **Network exposure** | Web server binds to `0.0.0.0` so it's reachable over **Tailscale**; access control delegated to Tailscale. Optional shared-token gate available in config (default off). |
| D12 | **Single process** | **One** process, one systemd unit. The web/dashboard and the music+relay logic live in the same process (web on its own thread, as in §6). No second process, no IPC socket, no polkit rule. |
| D13 | **Dashboard restart button** | The dashboard has a **Restart** button that restarts the **whole program**: clean shutdown (relays OFF, release audio) → process exits → `systemd Restart=always` respawns it. The dashboard is briefly offline during the restart; the **user manually refreshes the page** once the server is back. Relays re-init OFF on respawn. Same path the audio watchdog uses to escalate (§9). |
| D14 | **Analytics** | A persistent **analytics dashboard** is the landing page (§11.3): hours-of-day play histogram, favorite-songs ranking (tracks played to natural end), and per-day play counts. Backed by a **SQLite** event store (§11.4) written from the controller. Charts rendered with **Chart.js vendored as a local static asset** (no CDN, works offline). A failure in analytics must never affect playback/relays. |
| D15 | **UI language** | All **user-facing strings are in French** (HTML pages, button labels, and the `error`/`message` JSON the browser shows). **No localization framework** — French is hard-coded. **Internal log messages stay English** (operational, read via `journalctl` and the Debug log view). This is a standing convention for all new strings. |

---

## 3. Glossary

- **Press** — an *accepted* button activation, after debounce/validation. Distinct from a raw electrical edge.
- **Relay group** — the 3 relays. They are always switched together (all on / all off).
- **Track** — a playable audio file in the music folder.
- **Shuffle bag** — the ordered queue of tracks generated by shuffling, consumed one per press.
- **Mock mode** — running with simulated GPIO + audio (non-Pi, or forced for tests).
- **Real mode** — running on a Pi with real `gpiozero` + `pygame` audio.
- **Worker logic** — the in-process subsystem that owns GPIO, relays, audio, the button, selection, and the state machine. (Single process; not a separate OS process.)
- **Restart button** — dashboard control that restarts the worker logic; mechanism per §6.2.

---

## 4. Hardware & wiring (defaults, all configurable)

> Target board: **Raspberry Pi 4** (BCM GPIO numbering; `gpiozero` default pin factory).


| Function | GPIO (BCM) | Config | Notes |
|----------|-----------|--------|-------|
| Push button | 22 | `pull_up=True` (pull-up; pressed = LOW) | Was GPIO27 originally; 27 is dead on the user's board. Pull revised 2026-06-12, see below. |
| Relay 1 | 26 | `active_high=False`, `initial_value=False` | Active-low relay board. |
| Relay 2 | 20 | `active_high=False`, `initial_value=False` | |
| Relay 3 | 21 | `active_high=False`, `initial_value=False` | |

**Measured 2026-06-12 (ButtonScope, SPECS_BUTTONSCOPE.md):** the button is wired
**GPIO22 ↔ GND** with an external pull-up resistor on the line — pressing pulls the
line LOW. The original `pull_up=False` (internal pull-down) configuration *fought*
that external pull-up, parking the idle line at an ambiguous mid-level that read as
noisy-HIGH (= "pressed", with chatter): the root cause of the phantom presses.
With `pull_up=True` the line idles at a clean 3.3 V and presses are clean drops to
0 V (~4 sub-20 ms glitches per minute, rejected by the §8 hold-to-trigger window).

**Recommended hardware mitigation for phantom presses (see §8):**
- A pull resistor on the button line (gpiozero's internal pull is used by default; an external ~10 kΩ adds robustness).
- A ~100 nF capacitor across the button to ground → forms an RC low-pass that absorbs high-frequency EMI spikes.
- Keep button wiring **short**, and route it **away from relay and mains wiring** (the relays switching is the most likely noise source).

---

## 5. Behavior — state machine

Two primary states:

```
        ┌─────────────────────────── press (switch track) ──────────────────────────┐
        │                                                                            │
        v                                                                            │
   ┌─────────┐   press   ┌──────────┐   track ends naturally   ┌─────────┐           │
   │  IDLE   │ ────────► │ PLAYING  │ ───────────────────────► │  IDLE   │           │
   │relays   │           │relays ON │                          │relays   │           │
   │  OFF    │           │track #N  │ ◄────────────────────────┘                     │
   └─────────┘           └──────────┘                                                │
        ▲                     │                                                       │
        │                     └───────────────────────────────────────────────────--┘
        └── shutdown / cleanup always returns relays OFF
```

### Transitions

| From | Event | Action |
|------|-------|--------|
| IDLE | Accepted press | Select next track (shuffle bag); verify file exists on disk (§7.3); turn relays ON; start playback; fire webhook (async); → PLAYING |
| PLAYING | Accepted press | Stop current track; select next track **excluding the current one**; verify file; **leave relays ON**; start new playback; fire webhook; stay PLAYING |
| PLAYING | Track ends naturally | Turn relays OFF; → IDLE |
| any | Shutdown signal (SIGTERM/SIGINT) | Stop playback, turn relays OFF, release audio, exit cleanly |
| PLAYING | Audio subsystem failure detected | Watchdog recovers mixer (§9); if a track was meant to be playing, behavior per §9 |

### Notes
- **Relay invariant:** the relays are a *pure output* with no triggering logic of their own. Debounce / hold-to-trigger applies **only to the button input**. The relays turn ON solely as a consequence of an accepted button press starting playback, and turn OFF solely on natural track end, shutdown, or fault → safe-off. They never self-trigger and are never switched on by any path other than a press-initiated play.
- The relay group is ON **iff** state is PLAYING.
- "Track ends naturally" is detected by the playback supervisor (§9), not assumed from a timer.
- No use-case handling required for a track being deleted/renamed *while it is playing* (per requirements) — only a pre-play existence check (§7.3).

---

## 6. Process architecture & concurrency

The system runs as a **single process** / one systemd unit (D12). Inside it, the Flask dashboard runs on its own thread alongside the music+relay logic. All hardware events and timers funnel into a single **Controller** whose state transitions are serialized by one lock.

### Threads
1. **Button input thread** — detects accepted presses (real GPIO via gpiozero, or mock injection) and calls `controller.on_press()`.
2. **Playback supervisor thread** — polls playback status; on natural end → `controller.on_track_finished()`; also performs audio-health checks for the watchdog.
3. **Web server thread** — Flask app; web fake-press calls the same `controller.on_press()`; the Restart button calls the restart mechanism (§6.2).
4. **Watchdog** — may be folded into the playback supervisor thread; reinitializes audio on failure (§9).
5. **Main thread** — startup, signal handling, lifecycle, clean shutdown (relays OFF).

**Serialization rule:** `on_press()` (physical button or web) and `on_track_finished()` acquire the controller lock so events can't race. Webhook POSTs are fire-and-forget on a daemon thread (never block playback or relays). Relay writes are guarded (covered by the controller lock) so on/off can't interleave. The web server thread must never be able to take down playback/relay logic (failures isolated and guarded).

### 6.2 Dashboard Restart button (D13)

The Restart button restarts the **whole program** — the simplest mechanism, and the same one the audio watchdog uses to escalate (§9):

1. Browser → `POST /restart`.
2. Process performs a **clean shutdown**: stop playback, relays **OFF**, release audio, then **exit 0**.
3. `systemd Restart=always` respawns the process; relays re-initialize **OFF** (`initial_value=False`); the worker logic and dashboard come back up.
4. The dashboard is **briefly offline** (~2–5 s) during the restart; the **user manually refreshes the page** once the server is back (the page may also show a "restarting — refresh in a few seconds" hint).

**Why whole-program restart (not an in-process worker-only reset):** it's the least code (reuses the clean-shutdown path + systemd), and it recovers *every* failure mode — a wedged audio stack, a hung interpreter, leaked handles, stuck threads — not just the audio subsystem. A manual refresh after a few seconds is an accepted part of the flow.

> Note: this means the dashboard does **not** outlive a restart — that's intentional and accepted. Relay safety: a clean exit turns relays OFF; even a hard `SIGKILL` is self-healed because relays re-init OFF on the next start (note in the runbook).

---

## 7. Music selection & files

### 7.1 Music folder
- A known, configurable folder.
  - **Real mode** default is `/home/pi/musique` (explicit, matches the reference script's current behavior). Note: `~/musique` only equals `/home/pi/musique` when the process runs as user `pi`; under systemd `~` follows the configured `User=`, and modern Raspberry Pi OS (Bookworm+) has no default `pi` user — so the absolute path is used to avoid ambiguity. Override `music_folder` if the service runs as another user.
  - **Mock mode** uses a dedicated **`MockMusics/`** folder (project-relative, at the repo root) so debugging / unit tests / local runs draw from sample files the user provides there — never from the real Pi music folder. Configurable via `mock_music_folder`.
- Tracks discovered by extension: `.mp3`, `.wav`, `.ogg`, `.flac` (configurable).
- The folder is **expected to be stable** during operation. We do not watch it for live changes.
- On startup, if the folder has **zero** playable tracks → log an error and refuse to start playback (the service still runs so the dashboard is usable and reports the problem).

### 7.2 Shuffle-bag algorithm (D1)
- Build a list of all tracks; shuffle into a queue (the "bag").
- Each accepted press pops the next track from the bag.
- When the bag empties, reshuffle a fresh bag. **Constraint:** the first track of the new bag must differ from the just-played track (reshuffle or swap to enforce — handles the cycle boundary so no song plays twice in a row).
- **Switch-track case (PLAYING → press):** the newly selected track must differ from the currently playing one. Because the bag never repeats within a cycle and the boundary is guarded, this is automatically satisfied — but the selector still explicitly excludes the current track as a safety net (e.g. single-track folder → it just replays, logged).
- Edge cases:
  - **1 track:** every press (re)starts the same track; documented and logged, no crash.
  - **0 tracks:** see §7.1.

### 7.3 Pre-play existence check
- Immediately before loading a selected track, re-check the file exists on disk.
- If missing: log a warning, drop it from the current bag, and select the next candidate. If the bag/folder becomes empty as a result, fall back to IDLE with relays off and log an error.

---

## 8. Phantom-press / debounce strategy (D3)

> Scope note: this section concerns the **button input only**. The relays carry no triggering/debounce logic — they are a pure output (see the relay invariant in §5).

**Why this matters:** the previous build suffered phantom presses. On a Pi that switches relays, these are usually **EMI**, not contact bounce — a floating/weakly-pulled input, long antenna-like button wires, or spikes coupled from the relays switching. A single fixed-offset re-read can still be fooled by a spike that's high at both sample instants.

**Software approach — hold-to-trigger (sampled debounce):**
- Use gpiozero with a `bounce_time` to absorb contact chatter.
- Require the input to read **pressed continuously** across a short window before accepting (e.g. poll every ~5 ms and require ~10 consecutive "pressed" reads ≈ 50 ms; window configurable). A brief noise glitch cannot survive the window. This **subsumes** the old single stability re-check.
- Enforce a **minimum interval** between accepted presses (e.g. ≥ 300 ms; configurable).
- **Rapid-fire lockout:** if too many presses arrive in a short span (e.g. >3 within 1 s), apply a brief cooldown and log a warning (carried over from the reference script).
- After accepting a press, wait for release before re-arming (no auto-repeat while held).

**Hardware approach (documented recommendation, §4):** external pull resistor + ~100 nF RC filter across the button, short/shielded wiring routed away from relay & mains leads. Software treats the symptom; the cap kills the spike at the source.

All thresholds (hold-window ms, sample interval, min inter-press interval, rapid-fire limits) live in config.

---

## 9. Audio playback & watchdog (D7–D10)

### 9.1 Playback
- Default engine: `pygame.mixer` (matches reference; well understood on Pi). Abstracted behind an audio interface (§10) so it can be swapped/mocked.
- System default output device (D8). No volume control, no fade (D9): start plays immediately, switch/stop is an abrupt cut.
- Natural-end detection via the playback supervisor (polling `get_busy()` and/or pygame's end event).

### 9.2 Audio watchdog (the "audio dies after hours/days" problem)
**Observed failure:** after long uptime the audio subsystem wedges — playback stops working or "completely bugs."

**Detection (any of):**
- Playback was started but `get_busy()` never went true within a grace period.
- The mixer raises on `load`/`play`.
- A periodic lightweight health probe of the mixer fails.
- (Optional) A track that should still be playing reports not-busy far earlier than its known duration — ambiguous, so treated conservatively.

**Recovery:**
1. Log the detected fault with detail.
2. `pygame.mixer.quit()` then re-`init()` (re-open the audio device). Optionally re-init the whole pygame audio module.
3. If a track was supposed to be playing when the fault occurred: by default **return to IDLE with relays OFF** (safe state) rather than guess — the next button press starts fresh. (Configurable: optionally auto-resume/replay.)
4. If repeated re-inits fail beyond a threshold, escalate: log loudly, surface the unhealthy state on the dashboard, and (configurable) **exit non-zero so `systemd Restart=always` respawns the process** — recovering without a Pi reboot (D6). This is the same path as the dashboard **Restart** button under mechanism (A) (§6.2).

This watchdog is the lightweight first line (in-process re-init); a full restart (auto via the watchdog escalation / systemd, or manual via the dashboard button) is the heavier fallback. Both clear a wedged audio stack without rebooting the whole Pi.

### 9.3 Startup delay (D6)
- Keep a **configurable** startup delay (reference used `sleep(10)`) to let audio/network settle on boot. Default modest (e.g. 5–10 s); set to 0 for fast local/dev runs.

---

## 10. Platform abstraction & mock mode (G6)

The core logic must not import Pi-only libraries directly. Two thin adapters behind interfaces:

### 10.1 GPIO adapter
```
GpioBackend (interface)
  ├─ RealGpio   → gpiozero Button + OutputDevice
  └─ MockGpio   → in-memory button (inject presses) + relay state flags
```

### 10.2 Audio adapter
```
AudioBackend (interface)
  ├─ RealAudio  → pygame.mixer
  └─ MockAudio  → simulated playback with controllable/short durations; can simulate failure for watchdog tests
```

### 10.3 Platform detection
- Auto-detect Raspberry Pi (e.g. read `/proc/device-tree/model` or `/proc/cpuinfo` for "Raspberry Pi"; fall back to "can we import gpiozero with a real pin factory").
- Override via env var / CLI flag: force `--mock` (dev/laptop, CI) or `--real`.
- In mock mode the controller, selection, debounce, web dashboard, and watchdog logic all run identically; only the two adapters change. This is what makes the whole thing unit-testable.

> The dashboard runs in **both** modes (D2). In real mode it shows real relay state and drives real hardware; in mock mode the relay "lights" reflect simulated state and the fake-press is the only trigger.
>
> In mock mode, tracks are read from **`MockMusics/`** (see §7.1) — fill this folder with sample songs for debugging, local verification, and any tests that exercise real file discovery.

---

## 11. Web dashboard (G7, D2, D11)

- **Framework:** Flask (lightweight, ubiquitous on Pi). Single small app, minimal deps.
- **Bind:** `0.0.0.0:<port>` (default port configurable, e.g. 8080) so it's reachable from a laptop over **Tailscale** at `http://<pi-tailscale-name>:8080`.
- **Auth:** none by default (Tailscale provides network-level access control). Optional shared-token gate via config (D11).
- **Pages & navigation:** three pages, each with a header nav (token query string carried across, French labels):
  - `GET /` — **Analytics** landing page (§11.3). Links to *Debug* and *Chansons*.
  - `GET /debug` — the **operational dashboard** (formerly `/`): fake-press, relay status, live log, mode badge, restart. Links to *Accueil* (analytics) and *Chansons*.
  - `GET /songs` — song management (§11.1). Links to *Accueil* and *Debug*.
- **Debug page features** (moved verbatim from the old landing page, strings translated to French per D15):
  1. **Fake-press button** (« Appuyer ») → POSTs to `/press`, calls the same `controller.on_press()` as the physical button. Works alongside the physical button on the Pi.
  2. **Relay status** — three indicators colored by state (green = ON, grey = OFF), plus current state (IDLE/PLAYING) and current track name.
  3. **Live log view** — recent logs streamed to the page (ring buffer + Server-Sent Events). Helps debug phantom presses and audio faults. (Log lines stay English per D15.)
  4. **Mode badge** — shows REAL vs MOCK and whether GPIO/audio are live.
  5. **Restart button** (« Redémarrer ») → POSTs to `/restart`; restarts the whole program (§6.2).
- **Endpoints:**
  - `GET /` — the Analytics page (§11.4).
  - `GET /debug` — the operational dashboard page.
  - `POST /press` — inject a press.
  - `POST /restart` — clean-exit the process so systemd respawns it (§6.2).
  - `GET /status` — JSON: state, relays, current track, mode, watchdog/audio health.
  - `GET /events` (SSE) — log + status stream.
  - `GET /api/analytics` — JSON aggregates for the three charts (§11.3).
- Web server runs on its own thread inside the single process; its failures must never take down playback/relay logic (isolated, guarded).

### 11.1 Song management page

A second page for managing the music library from the browser. Navigation is a plain
**Songs** link in the dashboard header (token query string carried across) and a
**← Dashboard** link back.

- **Layout:** page title with an **Upload song** button at the top right; below it a card
  listing every playable file in the resolved music folder (name + file size), each row
  with a **Delete** button behind a `confirm()` dialog.
- **Restart to apply (cheap approach):** the track list is scanned once at startup (§7.1),
  so uploads/deletes take effect **only after a restart**. After the first successful
  change, the page shows a persistent *"changes take effect after restart"* notice with a
  **Restart** button (same `/restart` mechanism as §6.2). No live rescan.
- **Endpoints** (all behind the optional token gate of D11):
  - `GET /songs` — the page.
  - `GET /api/songs` — JSON list `{songs: [{name, size_bytes}], extensions, max_upload_bytes}`.
  - `POST /api/songs` — multipart upload (field `file`).
  - `DELETE /api/songs/<name>` — delete one file.
- **Upload safeguards:**
  1. Extension must be in `audio_extensions`; filename is run through
     `secure_filename()` (note: this ASCII-fies accents and replaces spaces).
  2. Size ≤ `upload_max_bytes` (config key, default 5 MB), enforced server-side via
     Flask `MAX_CONTENT_LENGTH` (JSON 413) **and** checked client-side before sending.
  3. Name collision with an existing file → **409, reject** ("delete it first") — never
     overwrite, never auto-rename.
  4. Atomic write: save to a `.part` temp name in the same folder, then rename — a
     half-written file can never be picked up by a startup scan.
- **Delete safeguards:**
  1. Path-traversal defense: bare filenames only — reject separators, `..` and leading
     dots, and verify the resolved path stays inside the music folder.
  2. Only files matching `audio_extensions` can be deleted (never arbitrary files).
  3. Deleting the currently-playing track is safe by design: the open file keeps playing
     (POSIX), and the pre-play existence check (§7.3) handles it vanishing afterwards.
  4. Deleting the last song is allowed; the page warns that the 0-track behavior of §7.1
     applies after restart.
- **Out of scope:** live rescan of the bag, audio-content validation (extension check
  only — an invalid file fails at play time, which the watchdog already tolerates),
  rename/reorder.

### 11.2 Clip selection & loudness normalization

Every upload is reduced to a short, loudness-normalized clip so all songs play at the
same intensity and length. Requires **ffmpeg + ffprobe** on the host (`apt install
ffmpeg` on the Pi, `brew install ffmpeg` on a dev Mac); when missing, the Songs page
shows a banner and upload/normalize are disabled (API returns **503**) — everything
else keeps working.

- **Client-side selection, server-side processing.** Picking a file opens a selector
  card *before* anything is uploaded: the browser previews the local file (Object URL)
  and decodes it with the Web Audio API to draw a waveform; a draggable window chooses
  the section (drag the body to move, drag an edge to resize), with Start and Length
  sliders alongside and a Preview button (plays the window, auto-stops). The clip length
  is variable per upload within `[clip_min_s, clip_max_s]` (default **10–30 s**), opening
  at `clip_duration_s`. If the browser can't decode the codec, the sliders alone still
  work. On confirm, the **full file + `start_s` + `duration_s`** are POSTed.
- **Server processing** (`saintantoine/processing.py`, no Flask imports):
  1. `ffprobe` the real duration; clamp `start_s` so the window fits. Songs shorter
     than the chosen clip length are kept whole.
  2. Two-pass EBU R128 `loudnorm` (pass 1 measures, pass 2 applies with
     `linear=true`): target `loudness_target_lufs` (default **-14 LUFS**), TP -1.5 dB,
     LRA 11.
  3. 0.3 s fade-in/out so the cut edges aren't abrupt (skipped on sub-second files).
  4. Encode by destination extension (mp3 → libmp3lame `-q:a 2`); album-art video
     streams dropped (`-vn`).
  5. All intermediate files (raw upload, encode output) use unique `.part` names
     inside the music folder — invisible to the startup scan, immune to concurrent
     uploads — and the final clip appears by atomic rename. ffmpeg calls run with a
     120 s timeout; failures clean up temps and surface as a JSON 500.
- **Normalize in place**: each song row has a **Normalize** button →
  `POST /api/songs/<name>/normalize` (same name-safety rules as delete). Same two-pass
  loudnorm over the whole file, no trim/fades, atomic replace. The filename is
  unchanged and tracks are opened at play time, so this needs **no restart** (the
  dirty/restart notice only applies to uploads/deletes).
- **Endpoint changes**: `POST /api/songs` requires form field `start_s` (float ≥ 0,
  else 400) and accepts optional `duration_s` (float > 0, else 400; clamped server-side
  to `[clip_min_s, clip_max_s]`; defaults to `clip_duration_s` when omitted); `GET
  /api/songs` additionally returns `clip_duration_s`, `clip_min_s`, `clip_max_s` and
  `ffmpeg_available`.
- **Config**: `clip_duration_s` (default 10.0, the selector's opening length),
  `clip_min_s` / `clip_max_s` (default 10.0 / 30.0, the selectable range; validated
  `0 < clip_min_s ≤ clip_duration_s ≤ clip_max_s`), `loudness_target_lufs` (default
  -14.0), and `upload_max_bytes` raised to **30 MB** since the full song is uploaded
  for trimming.

### 11.3 Analytics page (D14)

The landing page (`/`). Renders three charts with **Chart.js** (vendored locally at
`web/static/chart.min.js`, no CDN — works offline over Tailscale). All labels/titles are
in French (D15). The page is read-only; a failure to load analytics shows a French error
notice and never affects the rest of the app.

**Live updates (dynamic dashboard):** the page does an initial `GET /api/analytics`, then
subscribes to the existing SSE `/events` stream (which already emits a `status` event every
second). The controller's `status()` carries an **`analytics_rev`** counter that the event
store bumps on every recorded event; when the page sees the revision change it refetches
`/api/analytics` and updates the charts in place. So the graphs refresh within ~1 s of any
button press (physical or web) or natural song completion — without constant polling/redraws.

- **Graphe 1 — Heures d'écoute** ("hours of the day songs are most played"): a 24-bar
  histogram (hours 0–23) of **play-start** events (every accepted button press that starts
  or switches a song — physical or web). Answers *when* during the day music plays.
- **Graphe 2 — Chansons favorites** ("most favorite songs"): a horizontal bar ranking of
  tracks by **completed plays** — songs that played to their natural end (`on_track_finished`)
  — ordered most-played first, top N (default 10). Switching away from a track mid-play does
  **not** count (it wasn't played to the end).
- **Graphe 3 — Activité par jour** ("the day when most songs were played"): a per-calendar-day
  bar/time-series of play-start counts, with the **peak day highlighted** and called out in
  text (e.g. « Record : 42 lectures le 12 juin 2026 »). A button press counts as a play.
- **`GET /api/analytics`** returns, e.g.:
  ```json
  {
    "by_hour":  [{"hour": 0, "count": 3}, ...],          // 24 entries, hours 0–23
    "top_tracks": [{"name": "song.mp3", "count": 12}, ...],
    "by_day":   [{"day": "2026-06-12", "count": 42}, ...],
    "peak_day": {"day": "2026-06-12", "count": 42},
    "total_plays": 137, "total_completions": 88
  }
  ```

### 11.4 Analytics event store (D14)

A **SQLite** database (Python stdlib `sqlite3`, no new dependency) is the "simple database"
backing the analytics. New module `saintantoine/analytics.py`.

- **Schema** — one append-only table:
  ```sql
  CREATE TABLE IF NOT EXISTS events (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    ts    TEXT NOT NULL,    -- local wall-clock ISO-8601 (for hour/day grouping)
    type  TEXT NOT NULL,    -- 'play_started' | 'play_completed'
    track TEXT              -- basename; NULL allowed
  );
  CREATE INDEX IF NOT EXISTS idx_events_type_ts ON events(type, ts);
  ```
  Two derived buckets via `strftime('%H', ts)` (hour) and `strftime('%Y-%m-%d', ts)` (day).
- **Wall-clock vs monotonic:** the controller's playback timing uses `time.monotonic()`,
  which is **not** wall-clock and useless for "hour of day"/"which day". Analytics events are
  stamped with a separate **wall-clock** source (`datetime.now()`), injectable as `now_fn`
  for tests (independent of the controller's monotonic `clock`).
- **Recording hooks** (in `controller.py`, both physical and web presses flow through here):
  - `record_play_started(track)` — called from `_start_playback` right after a successful
    `load_and_play` (the single choke point for idle-start *and* switch). Feeds graphs 1 & 3.
  - `record_play_completed(track)` — called from `on_track_finished` (natural end only).
    Feeds graph 2.
- **Resilience (D14):** every DB call is wrapped so a failure is logged and swallowed —
  analytics must never raise into playback/relay logic, mirroring the webhook's
  fire-and-forget contract. A no-op `NullAnalytics` is the default injected object so the
  controller and all existing tests construct unchanged.
- **Thread-safety:** writes come from the supervisor/button threads, reads from the web
  thread. One `sqlite3` connection opened with `check_same_thread=False`, guarded by a
  `threading.Lock`; event volume is tiny (one row per press / per natural end).
- **Backend selection / location:** real mode stores at `analytics_db_path`
  (default `/home/pi/saintantoine/analytics.db`); mock mode resolves to a project-local
  file (same swap pattern as `resolve_log_file`, so dev runs don't write Pi paths).
  Disabled via `analytics_enabled: false` → `NullAnalytics`, page shows "analytics disabled".

---

## 12. Configuration

- Single config source with sane defaults overridable without code changes. **Proposed:** a `config.yaml` loaded into a typed config object, with environment-variable overrides for key fields (and `--mock/--real`, `--config` CLI flags).
- Configurable keys (non-exhaustive):
  - `music_folder` (real mode), `mock_music_folder` (default `MockMusics/`), `audio_extensions`
  - GPIO: `button_pin`, `button_pull_up`, `relay_pins`, `relay_active_high`, `relay_initial_value`
  - Debounce: `hold_window_ms`, `sample_interval_ms`, `min_press_interval_ms`, `rapidfire_count`, `rapidfire_window_s`, `rapidfire_cooldown_s`, `gpiozero_bounce_time_s`
  - Audio: `startup_delay_s`, watchdog thresholds (`health_probe_interval_s`, `play_start_grace_s`, `max_reinit_attempts`), `on_audio_fault` policy (`idle` | `resume`)
  - Web: `web_enabled`, `web_host` (default `0.0.0.0`), `web_port`, `web_auth_token` (optional), `upload_max_bytes` (§11.2, default 30 MB), `clip_duration_s` (§11.2, default 10 s), `clip_min_s` / `clip_max_s` (§11.2, default 10 / 30 s), `loudness_target_lufs` (§11.2, default -14)
  - Analytics (§11.4): `analytics_enabled` (default `true`), `analytics_db_path` (default `/home/pi/saintantoine/analytics.db`; mock mode swaps to a project-local file), `analytics_top_n` (default 10, favorites shown)
  - Webhook: `webhook_url` (empty = disabled), `webhook_timeout_s`
  - Logging: `log_file`, `log_level`, `log_ring_buffer_size`

---

## 13. Logging (G7)

- Structured timestamped logs (matches reference format: `%(asctime)s - %(levelname)s - %(message)s`).
- Sinks: rotating log file (config path; avoid unbounded growth) **and** stdout (so `journalctl` captures it under systemd) **and** an in-memory ring buffer feeding the dashboard.
- Log key events: accepted press, rejected press (with reason: debounce / parasite / rapid-fire), track selected, relays on/off, natural end, missing file, webhook result, audio fault + recovery, startup/shutdown.

---

## 14. Webhook (D5)

- If `webhook_url` is set: on each play start, POST `{"track": "<name>", "timestamp": "<ISO8601>"}` as JSON, fire-and-forget on a daemon thread with a short timeout. Never blocks playback or relays.
- If unset: feature disabled (no-op). Compatible with Airtable's generic webhook (the reference's existing target) or anything else.

---

## 15. Deployment — systemd (D6, G8)

- Ship **one** `systemd` unit (`saintantoine.service`):
  - **`Restart=always`** — required: it's what respawns the process after the dashboard **Restart** button (§6.2) and after an escalated audio fault (§9), as well as on ordinary crashes. (Pair with `RestartSec` and `StartLimit*` so a crash loop backs off instead of hammering.)
  - `ExecStart` runs the single process in real mode under the service user, with access to audio + GPIO.
  - Clean shutdown: handle `SIGTERM`/`SIGINT` → stop playback, relays OFF, release audio, exit. The `/restart` endpoint triggers the same clean-exit path. This makes `systemctl restart/stop saintantoine` and the dashboard Restart button work **without rebooting the Pi**.
- Document: enable on boot, `start`/`stop`/`restart`/`status`, and `journalctl -u saintantoine -f` for live logs.

---

## 16. Testing strategy (G6)

- **Framework:** `pytest`.
- **Unit tests (mock mode, no hardware):**
  - Shuffle bag: no immediate repeat, full coverage before repeat, boundary constraint, 1-track and 0-track edge cases.
  - State machine: IDLE→PLAYING on press; switch-track keeps relays on & changes track; natural end turns relays off; shutdown returns relays off.
  - Debounce: glitches rejected (sub-window spikes), genuine holds accepted, min-interval enforced, rapid-fire lockout.
  - Pre-play existence check: missing file is skipped; empty-folder fallback.
  - Audio watchdog: simulated failure triggers re-init; escalation after repeated failures triggers the clean-exit path; safe IDLE on fault.
  - Clean shutdown / restart path: `/restart` and SIGTERM both stop playback, turn relays OFF, release audio, and exit (mock backends assert relays ended OFF).
  - Webhook: disabled when no URL; payload shape; never blocks (failure tolerated).
  - Analytics (§11.4): events recorded on play-start and natural completion but **not** on
    mid-play switch; aggregation by hour / by day / top-tracks correct over an injected
    wall-clock; DB errors are swallowed (playback unaffected); `NullAnalytics` is a no-op;
    `GET /api/analytics` shape; controller hooks fire for both physical and web presses.
- **Local verification:** run with `--mock`, open the dashboard, click fake-press, watch relay colors + logs, confirm track switching and natural-end behavior using short mock-audio durations. Click **Restart**, confirm the process exits cleanly (relays OFF) and — under systemd or the dev runner — comes back; refresh the page to reconnect.
- **CI:** runs the mock-mode suite (no Pi, no audio device required).

---

## 17. Proposed repository layout

```
SaintAntoineV2/
├─ SPECS.md                      ← this document
├─ README.md
├─ MockMusics/                   ← sample tracks for mock mode (debug / local / tests)
├─ config.example.yaml
├─ pyproject.toml / requirements.txt
├─ saintantoine/
│  ├─ __init__.py
│  ├─ main.py                    ← entrypoint, lifecycle, signals, startup delay
│  ├─ config.py                  ← config load + defaults + env/CLI overrides
│  ├─ controller.py              ← state machine, serialization, press handling
│  ├─ selection.py               ← shuffle-bag selector
│  ├─ debounce.py                ← hold-to-trigger logic
│  ├─ audio.py                   ← AudioBackend + RealAudio(pygame) + MockAudio + watchdog
│  ├─ gpio.py                    ← GpioBackend + RealGpio(gpiozero) + MockGpio
│  ├─ platform.py                ← Pi detection / mode selection
│  ├─ webhook.py                 ← optional fire-and-forget webhook
│  ├─ analytics.py               ← SQLite event store + aggregation queries (§11.4)
│  ├─ logging_setup.py           ← file + stdout + ring-buffer handlers
│  └─ web/
│     ├─ server.py               ← Flask app, /press /restart /status /events /api/analytics
│     ├─ templates/              ← analytics.html (/), debug.html (/debug), songs.html
│     └─ static/                 ← chart.min.js (vendored Chart.js), icons
├─ deploy/
│  └─ saintantoine.service       ← systemd unit
└─ tests/
   ├─ test_selection.py
   ├─ test_controller.py
   ├─ test_debounce.py
   ├─ test_audio_watchdog.py
   └─ test_webhook.py
```

---

## 18. Resolved decisions (formerly open questions)

| # | Question | Decision |
|---|----------|----------|
| 1 | Music folder path | Real mode: **`/home/pi/musique`** (explicit). Mock mode: **`MockMusics/`** (project root). See §7.1. |
| 2 | Config format | **YAML** — `config.yaml` loaded into a typed object, env/CLI overrides. PyYAML dependency accepted. |
| 3 | Web port | **8080** (non-privileged), bound to `0.0.0.0` for Tailscale. |
| 4 | Audio-fault policy | **Safe-IDLE** — on watchdog recovery mid-song, relays off and wait for next press. (`on_audio_fault: idle`.) |
| 5 | Log location & rotation | Rotating file at **`/home/pi/saintantoine/log_script.txt`** (≈5 MB × 3) **+** stdout (journald) **+** in-memory ring buffer for the dashboard. |
| 6 | Service user | **`pi`** (already in `audio`/`gpio` groups). Override `User=` + `music_folder` if a different user is used. |
| 8 | Analytics storage | **SQLite** (stdlib `sqlite3`, single file, no new dependency) over a JSON log — proper aggregation queries, safe concurrent reads, trivial on a Pi. See §11.4. |
| 9 | Charting library | **Chart.js vendored locally** (`web/static/chart.min.js`, no CDN) over a CDN link (offline/Tailscale-only Pi) or hand-rolled SVG (less polished). See §11.3. |
| 10 | UI language | **French, hard-coded, no i18n framework** for user-facing strings; logs stay English. See D15. |
| 7 | Python version | Hardware confirmed **Raspberry Pi 4**. OS not yet confirmed; a Pi 4 most commonly runs Bookworm (Python 3.11). **Target 3.11**, keep code **compatible down to 3.9** (Bullseye). ⚠️ Still to verify when possible: `python3 --version` and `grep VERSION_CODENAME /etc/os-release`. |

---

## 19. Non-goals (explicitly out of scope)

- Handling a track being deleted/renamed *while it is actively playing*.
- Live watching of the music folder for changes during operation.
- Per-relay independent control (the 3 relays are always switched as one group).
- Volume control and fade in/out.
- User accounts / full auth on the dashboard (Tailscale handles network access).
- Analytics retention/pruning, CSV export, or date-range filtering (the event store grows
  unbounded; at button-press volume this is negligible for years). See §11.4.
- UI localization / multi-language support — French is hard-coded (D15).
