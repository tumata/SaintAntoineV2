#!/usr/bin/env bash
# Keep yt-dlp current (SPECS_YOUTUBE_IMPORT §8).
#
# YouTube breaks extraction periodically; yt-dlp ships fixes within days, so a
# stale copy stops working. Run this DAILY from the `pi` user's crontab, using
# the same interpreter the service runs under (/usr/bin/python3, no venv):
#
#   crontab -e        # as user pi
#   0 4 * * *  /home/pi/github/SaintAntoineV2/scripts/update-ytdlp.sh >> /home/pi/github/SaintAntoineV2/ytdlp-update.log 2>&1
#
# Override the interpreter with PYTHON=... if the app runs under a different one.
set -euo pipefail

PYTHON="${PYTHON:-/usr/bin/python3}"

# The [default] extra bundles yt-dlp-ejs (the JS "challenge solver" scripts
# YouTube's n-challenge needs alongside a JS runtime like Deno) — without it,
# extraction degrades. Keep both yt-dlp and the EJS bundle current.
#
# Raspbian/Debian mark the system Python "externally managed" (PEP 668); the
# plain install then needs --break-system-packages. Try clean first, fall back.
if ! "$PYTHON" -m pip install -U "yt-dlp[default]" 2>/dev/null; then
  "$PYTHON" -m pip install -U --break-system-packages "yt-dlp[default]"
fi

echo "$(date '+%Y-%m-%d %H:%M:%S') yt-dlp now $("$PYTHON" -m yt_dlp --version 2>/dev/null || echo '?')"
