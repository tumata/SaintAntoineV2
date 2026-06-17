# Volume Slider — Dashboard Master-Volume Control

## Specification & Requirements Document

> Status: **Implemented** · Last updated: 2026-06-17
>
> This document specifies a **dashboard volume slider** that tunes the Raspberry Pi's
> **overall (system master) output volume**, as an extension to the main Saint Antoine
> controller ([SPECS.md](SPECS.md)). It revises main-spec decision **D9** ("no volume
> control"), which points here.
>
> Shipped as [volume.py](saintantoine/volume.py) (`AmixerVolume`/`MockVolume`),
> `GET`/`POST /api/volume` in [web/server.py](saintantoine/web/server.py), the slider in the
> dashboard, config keys (§6, plus a `volume_min_pct` floor), and
> [tests/test_volume.py](tests/test_volume.py). The §9 open decisions landed as: a
> minimum-floor clamp (`volume_min_pct`, default 40), no cross-restart persistence (ALSA
> persists on its own), and `PCM` on card `0` as the configured mixer control for the Pi 4.

---

## 1. Goals

- **V1** — A slider on the main dashboard sets the Pi's **system-wide** output volume
  (0–100%), affecting *all* audio output, not just this app's playback.
- **V2** — The slider reflects the **current** system volume on page load.
- **V3** — Follow the repo's established patterns: a Real/Mock backend abstraction (like
  [audio.py](saintantoine/audio.py)), Flask endpoints behind the existing token gate,
  and full dev-machine (mock) operation with no Pi required.
- **V4** — A failure in the volume feature must **never** take down playback, relays, or
  the watchdog — same isolation rule as the rest of the web layer.

## 2. Key decision — what "overall volume" means

Control the **ALSA master mixer via `amixer`** (system-wide), **not** pygame's per-stream
volume.

- `pygame.mixer.music.set_volume()` only attenuates within our own playback stream and
  resets on each track load — it is *not* the Pi's overall volume.
- `amixer` changes the system mixer, which matches the user's intent ("the Raspberry Pi's
  overall volume") and is consistent with main-spec **D8** (system default output device).
- Trade-off accepted: this changes **system-global** state. See §7 (risks).

## 3. Backend — `saintantoine/volume.py` (proposed)

Mirror the `audio.py` Real/Mock pattern:

- **`VolumeControl` (ABC)** — `get() -> int | None`, `set(pct: int) -> None`.
- **`AmixerVolume(control, card)`** — real implementation, shells out via `subprocess`:
  - **get:** `amixer [-c <card>] sget <control>` → parse the `[NN%]` token from the output.
  - **set:** `amixer [-c <card>] sset <control> <pct>%` (clamp `pct` to 0–100).
  - All calls use a **timeout** (≈2 s) and catch `TimeoutExpired`, `CalledProcessError`,
    and `FileNotFoundError` (amixer missing) — logged, never raised. `get()` returns
    `None` on any failure.
- **`MockVolume(initial=50)`** — in-memory integer for mock mode and tests.
- **`create_volume_control(mode, cfg)`** — factory: `AmixerVolume` in real mode,
  `MockVolume` in mock mode (so the dev dashboard slider works without ALSA).

### 3.1 Mixer control name

The relevant ALSA control varies by Pi model and active output (commonly `Master`,
`PCM`, or `Headphone`). It is **configurable** (`mixer_control`, default `Master`). At
startup the app should log the available controls (`amixer scontrols`) once, so the
correct name is discoverable from the logs.

## 4. Web endpoints (behind the optional token gate, D11)

Add a `volume` backend to `create_app(...)` as a **keyword argument defaulting to a
`MockVolume`**, so existing call sites and tests stay valid (no signature break). Wire the
real backend in [main.py](saintantoine/main.py) via the factory.

- **`GET /api/volume`** → `{"volume": <int|null>, "enabled": <bool>}`
  (`volume` is `null` when the backend can't read the current level).
- **`POST /api/volume`** — JSON `{"volume": <int>}`:
  - Validate `0 ≤ volume ≤ 100` → **400** on missing/invalid/out-of-range/NaN input.
  - Apply via `volume.set`, log with `request.remote_addr` (matching the existing log
    style in [web/server.py](saintantoine/web/server.py)), return the applied value.
  - If the set fails at the ALSA layer, surface it so the UI can show "volume control
    unavailable" rather than silently doing nothing.

## 5. Frontend slider ([web/templates/index.html](saintantoine/web/templates/index.html))

- A **Volume** row inside the main `.card`: an `<input type="range" min="0" max="100">`
  plus a live `NN%` label, styled to match the existing dark theme.
- On load: `GET /api/volume`. If `enabled === false` or `volume === null`, hide/disable
  the control.
- On `input`: update the label live. On `change` (debounced ≈150 ms via `setTimeout`):
  `POST /api/volume`. Carries the `?token=` query string like the other fetches.

## 6. Configuration (new keys for §12 of the main spec)

| Key | Default | Meaning |
|-----|---------|---------|
| `volume_enabled` | `true` | Master enable for the slider + endpoints. |
| `mixer_control` | `"Master"` | ALSA simple-control name (`Master` / `PCM` / `Headphone`). |
| `mixer_card` | `""` | Optional ALSA card index/name; empty = default card. |

## 7. Risks & mitigations

- **`amixer` is a blocking subprocess on the Flask thread.** → 2 s timeout; web runs
  `threaded=True` so playback is never blocked; pile-ups are bounded by the timeout.
- **Wrong `mixer_control` → silent no-op** (most likely real-world issue: slider moves,
  nothing happens). → configurable name + startup `amixer scontrols` log + surface
  set-failures in the API/UI.
- **System-global footgun:** the slider changes the *whole Pi's* volume; setting it to 0
  silences the device with no physical recovery (headless Pi). → optional **minimum-floor
  clamp** (e.g. 5–10%) instead of true 0; or accept and document. (Open decision — see §9.)
- **Backward compatibility:** `volume` keyword arg with a `MockVolume` default keeps
  existing `create_app` callers and [tests/test_web.py](tests/test_web.py) valid.

### What this does *not* risk
Playback continuity, relay state, the watchdog/restart path, GPIO, and audio init are
fully isolated — volume control touches none of [controller.py](saintantoine/controller.py),
[audio.py](saintantoine/audio.py), or [gpio.py](saintantoine/gpio.py).

## 8. Testing strategy

- Update the `web` fixture in [tests/test_web.py](tests/test_web.py) to pass a `MockVolume`.
- `test_get_volume`, `test_set_volume` (200 + value applied to the mock),
  `test_set_volume_rejects_out_of_range` (400), invalid-JSON / missing-field (400).
- Unit test for `AmixerVolume` parsing of `amixer sget` output via a monkeypatched
  `subprocess.run` (no real ALSA in CI).

## 9. Open decisions

| # | Question | Notes |
|---|----------|-------|
| 1 | Minimum-floor clamp? | Clamp min to 5–10% to prevent silencing a headless Pi, vs. allow true 0. |
| 2 | Persist desired level across restarts? | ALSA volume persists on the Pi on its own; a startup re-apply from a small state file is optional and currently **out of scope**. |
| 3 | Default `mixer_control` for the target Pi 4? | Confirm whether it is `Master`, `PCM`, or `Headphone` on the deployed unit. |

## 10. Non-goals

- Per-track / in-stream volume (this is system master only).
- Fade in/out (still abrupt start/stop, per the surviving part of D9).
- Per-output (HDMI vs jack) routing or output-device selection.
