"""Optional fire-and-forget webhook (D5). Disabled when no URL is configured.

POSTs {"track": ..., "timestamp": ...} on each play start, on a daemon thread,
with a short timeout — never blocks playback or relays.
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.request
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)


class Webhook:
    def __init__(self, url: str = "", timeout_s: float = 5.0):
        self.url = url.strip()
        self.timeout_s = timeout_s
        self.enabled = bool(self.url)

    def fire(self, track_name: str) -> Optional[threading.Thread]:
        if not self.enabled:
            return None
        thread = threading.Thread(target=self._send, args=(track_name,), daemon=True)
        thread.start()
        return thread

    def _send(self, track_name: str) -> None:
        payload = json.dumps({
            "track": track_name,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }).encode("utf-8")
        request = urllib.request.Request(
            self.url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                log.info("Webhook sent (%s) for %s.", response.status, track_name)
        except Exception as e:
            log.error("Webhook failed for %s: %s", track_name, e)
