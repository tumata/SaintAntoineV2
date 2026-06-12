# ButtonScope — Live Button-Input Voltage Viewer

## Specification & Requirements Document

> Status: **Draft for review** · Last updated: 2026-06-12
>
> This document specifies a **standalone diagnostic tool**, independent from the main
> Saint Antoine controller ([SPECS.md](SPECS.md)), that observes the **button input line
> (GPIO22)** live and serves a **web page with a real-time voltage graph** showing the
> **last 10 seconds** of activity. Its purpose is to *see* what the software sees on the
> button line — genuine presses, contact bounce, and the phantom/EMI glitches that
> motivated the main spec's §8 debounce strategy — without attaching an oscilloscope.

---

## 1. Goals

- **B1** — Sample the button input line continuously and show it as a **live graph**
  in the browser, rendered as voltage (0 V / 3.3 V).
- **B2** — The graph **scrolls in real time** and always shows exactly the
  **last 10 seconds** (rolling window).
- **B3** — Follow the repo's established pattern: a simple Flask web server bound to
  `0.0.0.0`, reachable over Tailscale, minimal dependencies, no build step.
- **B4** — Run unmocked on the Pi *and* mocked on a dev laptop (synthetic signal),
  same as the main app (main spec G6).
- **B5** — Be **completely independent** of the main application: separate package,
  separate entrypoint, no imports from `saintantoine/` except the existing
  platform-detection helper. Never drives any output — **read-only** on the GPIO.
- **B6** — Help diagnose phantom presses: make sub-debounce glitches visible and
  countable.
- **B7** — The observed GPIO pin (and pull) is **reconfigurable live from the web
  page**, no restart needed.

---

## 2. Confirmed decisions

| # | Topic | Decision |
|---|-------|----------|
| BD1 | **Measurement method** | **Digital sampling** (user-confirmed). The Pi has no ADC; GPIO22 is read as a digital level at a fixed sample rate and rendered as 0 V / 3.3 V. True analog observation (ADC chip) is explicitly **out of scope** (§11). |
| BD2 | **Independence** | Standalone top-level package `buttonscope/` with its own `__main__`, own Flask app, own port. Not wired into the main systemd unit; run manually when debugging. |
| BD3 | **Pin contention** | On Raspberry Pi OS Bookworm (libgpiod/lgpio) a GPIO line can be claimed by **one process only**. ButtonScope therefore runs **instead of**, not alongside, the main service: `sudo systemctl stop saintantoine` first. If the pin claim fails at startup, exit with a clear message saying exactly that (§8). |
| BD4 | **Port** | Default **8081** (main dashboard owns 8080), so a stale dashboard tab never collides with the probe. |
| BD5 | **Transport** | **Server-Sent Events** (same pattern as the main dashboard's `/events`), pushing sample batches ~10×/s. Short-poll fallback not needed for a diagnostic tool. |
| BD6 | **Graph rendering** | Vanilla JS + `<canvas>` step plot. No charting library, no CDN, no build step — consistent with the main dashboard's minimal-deps approach, and works offline on the Pi. |
| BD7 | **Wire format** | **Edge list**, not raw samples. A square wave is fully described by its transitions; sending edges keeps SSE messages tiny even at high sample rates and gives exact glitch timestamps. See §6. |
| BD8 | **Live pin configuration** | The observed GPIO pin (and its pull) can be **changed from the web page without restarting** (user-confirmed preference). The sampler releases the current line and claims the new one at runtime; `--pin`/`--pull` only set the initial values. If the new claim fails, the old pin is kept and the error is shown in the UI (§5.1). |

---

## 3. Measurement principle & honest limitations

- A **sampler thread** polls the pin's raw digital value (no debounce, no
  `bounce_time`) at `sample_hz` (default **1000 Hz**, configurable). Each read is
  timestamped with a monotonic clock.
- The level is *rendered* as voltage: logic low → **0 V**, logic high → **3.3 V**.
  These are nominal values — the tool never measures actual volts (BD1).
- **What it can show:** press/release edges, contact bounce ≥ ~1 ms, EMI glitches
  that last at least one sample interval — i.e. exactly the class of events the
  debounce logic in main spec §8 must reject or accept.
- **What it can miss:** spikes shorter than the sample interval (~1 ms at 1 kHz).
  Python polling also has jitter; timestamps are recorded per-read (actual time, not
  assumed grid), so the graph is honest about *when* samples were taken.
  `sample_hz` may be raised (best effort, more CPU); the practical ceiling for a
  Python poller on a Pi 4 is a few kHz. For anything faster, use a real scope.
- **Wiring/pull configuration matches the main app's defaults** (button on GPIO22,
  pull-down), overridable via CLI flags (§7), so the probe sees the line under the
  same electrical conditions as the real controller.

---

## 4. Architecture

Single process, mirroring the main app's thread layout at miniature scale:

1. **Sampler thread** — owns the GPIO (or mock signal), polls at `sample_hz`,
   detects level transitions, and appends `(timestamp, level)` **edges** to a
   thread-safe **ring buffer** sized to hold a bit more than the display window
   (default: 15 s of edges). Also keeps running counters (§5 stats).
2. **Web server thread** — Flask app: serves the page, the SSE stream, and a JSON
   status endpoint. A web-thread failure must never crash the sampler.
3. **Main thread** — argument parsing, mode selection (real/mock), signal handling
   (`SIGINT`/`SIGTERM` → close GPIO, exit 0).

**GPIO access (real mode):** `gpiozero.DigitalInputDevice` (lazy import, Pi only)
with the configured pull, **no** `bounce_time` — raw line, no filtering, read-only.
No relay objects are ever created (B5).

---

## 5. Web UI

One page (`GET /`):

- **Live graph** — `<canvas>` step plot, x-axis = last **10 s** (right edge = now,
  scrolling continuously via `requestAnimationFrame`), y-axis = 0–3.3 V with the two
  logic levels marked. Edges from the SSE stream are drawn as vertical steps; pulses
  shorter than `glitch_ms` are highlighted in a warning color so phantom blips jump
  out visually.
- **Stats bar** — current level (HIGH/LOW), edge count, **glitch count** (pulses
  shorter than `glitch_ms`, default 20 ms — i.e. would-be-rejected by the main app's
  ~50 ms hold-to-trigger window), press count (pulses ≥ `glitch_ms`), sample rate
  actually achieved (measured, not configured). Counters since start, with a
  **Reset counters** button.
- **Mode badge** — REAL vs MOCK, pin number, pull configuration (same convention as
  the main dashboard).
- **Pause/Resume** button — freezes the graph for inspection; sampling and counters
  continue underneath; resume snaps back to live.
- **Pin configuration panel** — see §5.1.

No restart button, no auth beyond the Tailscale assumption (D11 in the main spec
applies: bind `0.0.0.0`, network access controlled by Tailscale).

### 5.1 Live pin configuration (BD8)

A small panel on the page: **pin number** input (BCM), **pull** selector
(down / up / none), and an **Apply** button.

- Apply → `POST /config` with `{pin, pull}`. The sampler thread, between two polls:
  1. closes the current input device (releases the line),
  2. claims the new pin with the new pull,
  3. **clears the graph window and resets the counters** — they described the old
     line and would be misleading on the new one.
- **On failure** (pin busy, invalid BCM number, claim error): the sampler **re-claims
  the previous pin** and the response carries the error; the UI shows it inline
  (e.g. *"GPIO17 is busy — kept watching GPIO22"*) and the panel reverts. If even
  re-claiming the old pin fails (another process grabbed it in the gap), the sampler
  enters a visible **NO PIN** error state on the page rather than crashing.
- The mode badge and `GET /status` always reflect the **currently active** pin/pull,
  never the requested one.
- In **mock mode** the panel works too (the synthetic generator just relabels its
  pin and resets), so the flow is testable off-Pi.
- Concurrency: pin swap is serialized with the polling loop by the sampler's lock —
  no reads can interleave with a half-swapped device.

---

## 6. Endpoints & wire format

| Endpoint | Purpose |
|----------|---------|
| `GET /` | The graph page (template + inline/static JS). |
| `GET /events` | SSE stream of edge batches. |
| `GET /status` | JSON: mode, **active** pin, pull, sample_hz configured + achieved, counters, current level. |
| `POST /config` | `{pin, pull}` — switch the observed line live (§5.1). 200 with new status, or 4xx/5xx with `{error}` and the kept configuration. |
| `POST /reset` | Reset the counters (not the graph). |

**SSE message (every ~100 ms):**

```json
{
  "now": 1234.567,                       // server monotonic time, seconds
  "level": 1,                            // current level (lets client draw with zero edges)
  "edges": [{"t": 1234.501, "v": 1}],    // transitions since last message (may be empty)
  "counters": {"edges": 42, "glitches": 3, "presses": 7, "achieved_hz": 998.4}
}
```

The client keeps its own 10 s edge window, reconstructs the square wave from edges +
current level, and maps server monotonic time to local render time using the `now`
field (re-synced on every message, so clock drift can't accumulate). On SSE
reconnect the client clears its window and starts fresh — acceptable for a live
diagnostic view.

---

## 7. Configuration (CLI flags only)

An independent diagnostic tool doesn't need the YAML config machinery. Flags, with
defaults matching the main app's wiring (main spec §4):

| Flag | Default | Meaning |
|------|---------|---------|
| `--pin` | `22` | **Initial** BCM pin to observe — changeable live from the web page (§5.1). |
| `--pull` | `up` | **Initial** pull: `down` / `up` / `none` — changeable live (§5.1). Default matches the main app's wiring (button to GND, main spec §4 as revised 2026-06-12 — a revision this very tool produced). |
| `--port` | `8081` | Web port. |
| `--host` | `0.0.0.0` | Bind address. |
| `--sample-hz` | `1000` | Target polling rate (best effort). |
| `--window-s` | `10` | Graph window (kept as a flag, but 10 s is the spec'd default — B2). |
| `--glitch-ms` | `20` | Pulses shorter than this count as glitches and are highlighted. |
| `--mock` / `--real` | auto-detect | Force mode; auto-detection reuses `saintantoine.platform_detect`. |

---

## 8. Coexistence with the main service

- **The probe and the main service cannot watch the same pin at once** (BD3). The
  documented workflow is:
  ```bash
  sudo systemctl stop saintantoine
  python -m buttonscope            # observe, debug…
  sudo systemctl start saintantoine
  ```
- If claiming the pin fails at startup, exit non-zero with:
  *"GPIO22 is busy — is the saintantoine service running? Stop it first:
  `sudo systemctl stop saintantoine`."*
- The probe **can** run while the service is up if it observes a different, free pin
  (picked at launch with `--pin` or switched live per §5.1) — only the *same* line
  is exclusive. Attempting to switch onto a busy pin live fails gracefully (§5.1).
- No systemd unit for ButtonScope — it's an on-demand tool, run from an SSH session
  (or `tmux`), not a boot service.
- Port 8081 means both web UIs can be bookmarked side by side without conflict.

---

## 9. Mock mode (B4)

- Auto-detected the same way as the main app (reuse `platform_detect`); forced with
  `--mock` / `--real`.
- The mock signal generator replaces the GPIO poller and produces a deterministic-ish
  synthetic waveform so the UI is fully developable off-Pi:
  - idle LOW;
  - a "press" every ~3 s (HIGH for 300–800 ms) with a few milliseconds of simulated
    contact bounce on each edge;
  - an occasional sub-`glitch_ms` spike (~every 5 s) so the glitch highlighting and
    counter are visibly exercised.
- Everything downstream of the sampler (ring buffer, edge encoding, SSE, UI, stats)
  is identical in both modes — that's what makes it testable.

---

## 10. Repository layout & testing

```
SaintAntoineV2/
├─ SPECS_BUTTONSCOPE.md          ← this document
├─ buttonscope/
│  ├─ __init__.py
│  ├─ __main__.py                ← CLI flags, mode selection, lifecycle, signals
│  ├─ sampler.py                 ← poller thread, edge detection, ring buffer, counters, mock signal
│  └─ web.py                     ← Flask app: /, /events (SSE), /status, /reset + template
└─ tests/
   └─ test_buttonscope.py        ← see below
```

- Registered as a second console entry point / runnable via `python -m buttonscope`.
  No new dependencies (Flask is already required by the main app; gpiozero is
  Pi-only and lazily imported, as in `saintantoine/gpio.py`).
- **Tests (pytest, mock mode, no hardware):**
  - Edge detection: a level sequence produces the correct edge list with correct
    timestamps.
  - Ring buffer: edges older than the retention window are evicted; window queries
    return exactly the last N seconds.
  - Glitch classification: pulse < `glitch_ms` → glitch counter; ≥ → press counter.
  - SSE/status endpoints (Flask test client): message shape per §6, counters reset
    via `POST /reset`.
  - Live pin swap (mock): `POST /config` switches the pin, clears window + counters,
    and status reports the new pin; a simulated claim failure keeps the old pin and
    returns the error.
  - Mock signal generator: produces presses, bounce, and glitches at the documented
    cadence.

---

## 11. Non-goals (explicitly out of scope)

- **Real analog voltage measurement** — no ADC hardware (BD1). The y-axis volts are
  nominal logic levels.
- Catching spikes shorter than the sampling interval (use a real oscilloscope).
- Running concurrently with the main service on the same pin (BD3).
- Recording/exporting traces to disk (screenshot the graph if needed).
- Auth, multi-pin observation, relay monitoring, or any write access to GPIO.
- History beyond the rolling window — the page shows the last 10 s, nothing more.
