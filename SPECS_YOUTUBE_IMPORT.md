# YouTube Import — Paste a Link, Download Audio, Trim It

## Specification & Requirements Document

> Status: **Implemented** · Last updated: 2026-06-17
>
> Shipped on branch `youtube-import`: [youtube.py](saintantoine/youtube.py),
> `POST /api/youtube` in [web/server.py](saintantoine/web/server.py), the import bar in
> [web/templates/songs.html](saintantoine/web/templates/songs.html), config keys (§6),
> [scripts/update-ytdlp.sh](scripts/update-ytdlp.sh), and tests in
> [tests/test_web.py](tests/test_web.py). Live-verified end-to-end against real YouTube
> (download, title parsing, match-filter rejection, HTTP error paths).
>
> **Real-world note:** recent `yt-dlp` warns "No supported JavaScript runtime could be found —
> extraction without one has been deprecated." Downloads still work degraded; installing a JS
> runtime (e.g. `deno`) on the Pi is recommended for long-term robustness.
>
> This document specifies a **YouTube import** flow for the Songs page: the user pastes a
> YouTube URL, the Raspberry Pi downloads the audio as MP3, and the existing client-side
> waveform trimmer takes over so the user clips it before it is saved. It is an extension
> to the main Saint Antoine controller ([SPECS.md](SPECS.md)) and the existing upload/trim
> flow in [web/server.py](saintantoine/web/server.py) and
> [web/templates/songs.html](saintantoine/web/templates/songs.html). **No code has been
> written.**

---

## 1. Goals

- **Y1** — A field on the Songs page accepts a **YouTube URL**; submitting it downloads the
  track's audio as MP3 **on the Pi** and hands it to the existing trim UI.
- **Y2** — **Reuse the existing trim/normalize pipeline unchanged.** The download produces a
  browser `File`, which is fed to the current `openSelector(file)` flow; confirming re-uploads
  the chosen clip to the existing `POST /api/songs`. No new trim logic, no new trim endpoint.
- **Y3** — Follow the repo's established patterns: a subprocess wrapper module mirroring
  [processing.py](saintantoine/processing.py), Flask endpoints behind the existing token gate,
  graceful degradation when the tool is missing, and full mock-mode operation for dev/tests.
- **Y4** — A failure in the import feature must **never** take down playback, relays, the
  watchdog, or the rest of the web layer — same isolation rule as the rest of the dashboard.
- **Y5** — All user-facing strings are **French**; logs stay English (repo convention).

## 2. Key decision — why this must run server-side (on the Pi)

The download **cannot** be done in the browser. It runs in `yt-dlp` on the Pi.

- **CORS:** YouTube media is served from `googlevideo.com` with CORS restrictions; a browser
  `fetch()` from the dashboard origin is blocked. `yt-dlp` is not a browser and is not subject
  to CORS.
- **Signature/n-sig cipher:** YouTube obfuscates stream URLs with a JS cipher that changes
  regularly; `yt-dlp` reverse-engineers it and ships frequent updates. Reimplementing this in
  the page is infeasible and would break constantly.
- **"Server-side" here means the Pi itself** — the Flask app is already the local server. No
  third party is involved; the download happens on the user's own hardware over their LAN.

The trimming **stays client-side** (the existing Web Audio waveform UI). Only fetch+extract
moves to the Pi, because that is the only place it can work.

## 3. Handoff architecture — round-trip via the client

```
browser            Pi (Flask)              YouTube
  │  POST /api/youtube {url}  │                 │
  │ ─────────────────────────>│  yt-dlp ───────>│   download + extract mp3
  │   mp3 bytes + X-Song-Title│<────────────────│
  │<──────────────────────────│ (temp file deleted)
  │  wrap blob in File → openSelector(file)     │   ← existing waveform trim UI
  │  POST /api/songs (file + start_s + duration_s)   ← existing trim/normalize path
```

The full song travels server→client once, then the **trimmed clip** travels client→server.
A typical 4-minute MP3 at ~192 kbps is ~5–7 MB — well under `upload_max_bytes` (30 MB), so the
re-upload passes; the duration/filesize caps (§6) keep pathological inputs out. This reuses
100% of the trim path and avoids any server-side temp-file lifecycle/streaming/trim-by-id
machinery.

## 4. Backend — `saintantoine/youtube.py` (proposed)

Mirror the subprocess style of [processing.py](saintantoine/processing.py) (a `_run`-style
helper, a typed error, an availability probe):

- **`YoutubeError(Exception)`** — analogous to `processing.ProcessingError`. Carries the
  `yt-dlp` stderr tail so the endpoint can produce a useful message.
- **`youtube_available() -> bool`** — `shutil.which("yt-dlp") is not None`. (ffmpeg/ffprobe,
  required for MP3 extraction, are already a dependency of [processing.py](saintantoine/processing.py).)
- **`is_youtube_url(url: str) -> bool`** — parse with `urllib.parse`; accept **only** hosts
  `youtube.com`, `www.youtube.com`, `m.youtube.com`, `music.youtube.com`, `youtu.be` over
  `http`/`https`. Everything else is rejected before any subprocess runs (limits SSRF / abuse
  surface — see §7).
- **`download_audio(url, dest_dir, *, max_duration_s, max_filesize, timeout_s) -> (Path, str)`**
  — runs `yt-dlp` via `subprocess.run(capture_output=True, text=True, timeout=timeout_s)`:
  - Flags: `--no-playlist -x --audio-format mp3 --audio-quality 2`,
    `--max-filesize <max_filesize>`, `--match-filter "duration < <max_duration_s>"`,
    `--no-progress`, `-o "<dest_dir>/%(title)s.%(ext)s"`.
  - Returns the resulting `.mp3` path and the **video title** (read from the produced filename,
    or via `--print` / `--no-simulate` if cleaner).
  - On non-zero exit, timeout, or `FileNotFoundError`, raise `YoutubeError` (logging the stderr
    tail, matching `processing._run`).
  - **No auto-update / retry logic** lives here — keeping the tool current is the cron job's job
    (§8).

## 5. Web endpoint — `POST /api/youtube` (behind the optional token gate, D11)

Add a `youtube` capability to `create_app(...)`; gate the route on config so existing call
sites/tests stay valid.

- Guard order: `youtube_enabled` (else 503), `youtube_available()` (else 503), JSON body with a
  string `url` (else 400), `is_youtube_url(url)` (else 400).
- Download into a `tempfile.TemporaryDirectory()`; on success return the MP3 via `send_file`
  with `mimetype="audio/mpeg"` and the sanitized title in an **`X-Song-Title`** response header
  (exposed via `Access-Control-Expose-Headers` if needed). The temp dir is removed in a
  `finally` block — **nothing is persisted server-side** by this endpoint.
- Failure mapping (French messages, English logs incl. `request.remote_addr`, matching the
  existing upload handler):
  - missing/disabled tool → **503** "L'import YouTube n'est pas disponible sur le serveur."
  - bad / non-YouTube URL → **400** "Lien YouTube invalide."
  - `YoutubeError` (download/extract failed) → **502** "Échec du téléchargement — réessayez ;
    si le problème persiste, yt-dlp est peut-être à mettre à jour." (the update hint is the
    backstop between weekly cron runs — §8).
- Extend the existing **`GET /api/songs`** payload ([web/server.py](saintantoine/web/server.py))
  with `youtube_available` and `youtube_enabled` so the UI can show/hide the importer.

## 6. Configuration (new keys for §12 of the main spec)

| Key | Default | Meaning |
|-----|---------|---------|
| `youtube_enabled` | `true` | Master enable for the import field + endpoint. |
| `youtube_max_duration_s` | `900` | Reject videos longer than ≈15 min via `--match-filter` (covers any song, rejects albums/podcasts). |
| `youtube_max_filesize` | `"50M"` | `yt-dlp --max-filesize` cap; stays well under `upload_max_bytes` on re-upload. |
| `yt_dlp_timeout_s` | `90` | Hard subprocess timeout for a download. |

> ⚠️ **Pi config drift:** the deployed Pi's `config.yaml` is maintained separately and lags the
> example. These keys must be added there too when this ships (standing repo reminder).

## 7. Risks & mitigations

- **`yt-dlp` rots as YouTube changes.** Breakage is the main ongoing cost. → Weekly cron update
  (§8); unpinned/`>=` dependency so updates take effect; the 502 message hints at updating for
  the gap between runs.
- **Synchronous download blocks a Flask worker (~seconds to ~1 min).** → Hard `yt_dlp_timeout_s`;
  web runs `threaded=True` so playback is never blocked; the UI shows a "Téléchargement…" state
  so the wait is explained, not hung.
- **Arbitrary-URL fetch / SSRF / unexpected content.** → Host allowlist in `is_youtube_url`
  (YouTube hosts only) **before** any subprocess; `--no-playlist`; duration + filesize caps.
- **Double byte round-trip / large files.** → Caps in §6 keep sizes sane; a normal song is far
  under `upload_max_bytes`; oversized inputs fail fast at download, not at re-upload.
- **Filename safety & collisions.** → The title is run through `secure_filename` (as the existing
  upload path already does at [web/server.py](saintantoine/web/server.py)) before it becomes a
  `File` name / destination. On a name clash the client **auto-dedupes** with a numeric suffix —
  ` (2)`, ` (3)`, … — so an import never dead-ends on the existing 409; see §9.

### What this does *not* risk
Playback continuity, relay state, the watchdog/restart path, GPIO, and audio init are fully
isolated **at the thread level** (verified): the button detector, the audio supervisor, and the
web server each run on their own daemon threads ([main.py:119-130](saintantoine/main.py#L119-L130)),
Flask is `threaded=True`, and `run_web_server` swallows every exception so a web failure can never
propagate ([server.py:292-299](saintantoine/web/server.py#L292-L299)). A hung, erroring, or
throwing import request therefore cannot block or crash the button or playback. The
**trim/normalize pipeline is untouched** — `processing.py` and `POST /api/songs` are reused as-is.

### 7.1 Hardening for crash-isolation (mandatory — primary requirement)

Thread isolation does **not** cover shared OS resources. The download is heavy and runs on the
same Pi as the core loop, so the following are **requirements**, not nice-to-haves:

- **Disk-fill guard.** Before downloading, check free space via `shutil.disk_usage(music_folder)`
  (already used at [server.py:149](saintantoine/web/server.py#L149)) and refuse with a French 507/503
  message if free space `< youtube_max_filesize × safety_factor` (e.g. ×3). Download into a temp
  dir on the **same filesystem** as the music folder; remove it in a `finally` block whatever
  happens (success, error, timeout). A full SD card is the most likely way this feature could
  harm the Pi — this guard is the headline mitigation.
- **Process-group kill on timeout.** Launch `yt-dlp` with `start_new_session=True` (own process
  group); on `TimeoutExpired` send `SIGTERM` then `SIGKILL` to the **whole group** so the
  `ffmpeg` grandchild yt-dlp spawns cannot be orphaned and keep consuming CPU/disk. A bare
  `subprocess.run(timeout=)` is insufficient here.
- **Low scheduling priority.** Run the subprocess niced (`nice -n 10`, and `ionice -c3` where
  available) so transcoding yields CPU/IO to the audio playback, the button thread, and the audio
  supervisor's health probe — preventing both audio stutter and a false watchdog escalation.
- **Single-flight concurrency lock.** A module-level lock allows **one** import at a time; a
  concurrent request returns **429** "Un import est déjà en cours." This bounds peak CPU/disk to
  one download regardless of how many browser tabs hit the button.
- **Total-failure containment.** The endpoint wraps the whole flow in `try/except Exception`,
  logs, and returns a French 5xx — never re-raises. (Flask would catch it anyway; this is
  belt-and-suspenders so a download failure is always a clean error, never a stack trace to the
  user or a leaked temp file.)

None of the above touches `controller.py`, `audio.py`, `gpio.py`, or the watchdog — they are
confined to `youtube.py` and the new endpoint.

## 8. Keeping `yt-dlp` current — daily cron at 04:00 (on the Pi)

Updating is handled out-of-band, **not** on the request path (no on-failure auto-update: it would
block a request for the duration of a pip install, and most failures aren't staleness).

The Pi runs the app via systemd as **`User=pi`** with the **system interpreter `/usr/bin/python3`**
(no venv — [deploy/saintantoine.service:22-24](deploy/saintantoine.service#L22-L24)), and `pi`'s
user crontab is the established cron location ([deploy/LEGACY_V1.md](deploy/LEGACY_V1.md)). So
`yt-dlp` lives in that interpreter's environment and the update runs from `pi`'s crontab against
the **same `/usr/bin/python3`**.

- **Daily at 04:00**, in `pi`'s crontab (`crontab -e` as `pi`):
  ```cron
  0 4 * * *  /home/pi/github/SaintAntoineV2/scripts/update-ytdlp.sh >> /home/pi/github/SaintAntoineV2/ytdlp-update.log 2>&1
  ```
- Provide `scripts/update-ytdlp.sh`: runs `/usr/bin/python3 -m pip install -U yt-dlp`. On
  Raspbian/Debian with PEP 668 (externally-managed environment) this needs
  `--break-system-packages` (or `--user`); the helper handles that so the crontab line stays
  trivial. Include a README/deploy note next to the install steps and the §6 config-drift
  checklist.

## 9. Frontend ([web/templates/songs.html](saintantoine/web/templates/songs.html))

- In the header, next to "Ajouter une chanson", add a URL `<input type="url">` plus an
  **"Importer depuis YouTube"** button. Shown only when `youtube_available && youtube_enabled`
  (from the `GET /api/songs` payload, §5).
- On submit: disable the control, show "Téléchargement…", `POST /api/youtube {url}` carrying the
  `?token=` query string like the other fetches.
- On success: read the response `Blob` + `X-Song-Title` header →
  `new File([blob], title + ".mp3", {type: "audio/mpeg"})` → call the **existing**
  `openSelector(file)` ([songs.html:293](saintantoine/web/templates/songs.html#L293)). The
  waveform, sliders, and preview are entirely unchanged from there.
- **Collision auto-dedupe:** at confirm, the client checks the chosen filename against the loaded
  song list (already in memory from `GET /api/songs`) and, if taken, appends ` (2)`, ` (3)`, …
  before uploading, so a YouTube import never hits the 409 dead-end. (Local-file uploads keep
  their current behaviour.)
- On error: show the server's French message via the existing `showError`, re-enable the field.

## 10. Resolved decisions

| # | Question | Decision |
|---|----------|----------|
| 1 | Cron interpreter / user | `pi`'s crontab, system `/usr/bin/python3` (no venv), via `scripts/update-ytdlp.sh`. **Daily at 04:00** (§8). |
| 2 | Default caps | `youtube_max_duration_s = 900` (15 min), `youtube_max_filesize = "50M"` (§6). |
| 3 | Host allowlist | YouTube hosts only, **including** `music.youtube.com` and `youtu.be` short links (§4). |
| 4 | Title → filename conflicts | **Auto-dedupe** client-side with a ` (2)`, ` (3)`, … suffix; never dead-end on 409 (§7, §9). |

## 11. Testing strategy

- Monkeypatch `youtube.download_audio` (as `processing.process_clip` is already mocked) — no real
  network or `yt-dlp` in CI.
- `POST /api/youtube`: success (returns bytes + `X-Song-Title`), disabled → 503, missing tool →
  503, non-YouTube/invalid URL → 400, `YoutubeError` → 502.
- Unit tests for `is_youtube_url` (accept/reject host matrix) and for `download_audio` flag
  assembly via a monkeypatched `subprocess.run`.
- `GET /api/songs` includes `youtube_available` / `youtube_enabled`.
- **Safety guards (§7.1):** low free space → refused before any subprocess runs (monkeypatch
  `shutil.disk_usage`); a second concurrent import → **429** while the lock is held; a simulated
  timeout invokes the process-group kill path; temp dir is removed on success **and** on every
  failure branch (assert no `.part`/temp left behind).

## 12. Non-goals

- Server-side temp-file caching, streaming, or a trim-by-id endpoint (rejected in favour of the
  client round-trip, §3).
- On-failure auto-update / retry of `yt-dlp` (rejected in favour of the weekly cron, §8).
- Non-YouTube sources (SoundCloud, Vimeo, arbitrary `yt-dlp` sites) — host allowlist is
  YouTube-only by design (§7).
- Background/async download jobs or progress bars — a single synchronous request with a timeout
  is sufficient for a single-user dashboard.
- Any change to the trim, normalization, or playback behaviour.
