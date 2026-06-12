"""Flask app: graph page, SSE edge stream, status, live pin config, reset.

SPECS_BUTTONSCOPE §5, §6. Same conventions as the main dashboard: bind
0.0.0.0 (Tailscale handles access), web failures never crash the sampler.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Tuple

from flask import Flask, Response, jsonify, render_template, request

from .sampler import PULLS, Sampler

log = logging.getLogger(__name__)

SSE_INTERVAL_S = 0.1
BCM_PIN_RANGE = range(0, 28)


def sse_payload(sampler: Sampler, since_seq: int) -> Tuple[dict, int]:
    """One SSE message (§6): edges since `since_seq` + current state."""
    snap = sampler.snapshot_since(since_seq)
    latest = snap.pop("latest_seq")
    return snap, latest


def create_app(sampler: Sampler, mode: str, window_s: float = 10.0) -> Flask:
    app = Flask(__name__)

    def full_status() -> dict:
        status = sampler.status()
        status["mode"] = mode
        status["window_s"] = window_s
        return status

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/status")
    def status():
        return jsonify(full_status())

    @app.post("/config")
    def config():
        data = request.get_json(silent=True) or {}
        pin = data.get("pin")
        pull = data.get("pull")
        if not isinstance(pin, int) or isinstance(pin, bool) or pin not in BCM_PIN_RANGE:
            return jsonify({"error": "pin must be a BCM number between 0 and 27.",
                            "status": full_status()}), 400
        if pull not in PULLS:
            return jsonify({"error": "pull must be one of: %s." % ", ".join(PULLS),
                            "status": full_status()}), 400
        ok, err = sampler.set_input(pin, pull)
        if not ok:
            return jsonify({"error": err, "status": full_status()}), 409
        log.info("Pin config changed from web (%s): GPIO%d pull-%s.",
                 request.remote_addr, pin, pull)
        return jsonify({"ok": True, "status": full_status()})

    @app.post("/reset")
    def reset():
        sampler.reset_counters()
        return jsonify({"ok": True, "status": full_status()})

    @app.get("/events")
    def events():
        def generate():
            seq = 0  # replay the buffered window on connect
            while True:
                payload, seq = sse_payload(sampler, seq)
                yield f"event: sample\ndata: {json.dumps(payload)}\n\n"
                time.sleep(SSE_INTERVAL_S)

        return Response(generate(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache",
                                 "X-Accel-Buffering": "no"})

    return app


def run_web_server(app: Flask, host: str, port: int) -> None:
    """Thread target. A web failure must never take down the sampler."""
    try:
        log.info("ButtonScope on http://%s:%d (reachable over Tailscale).", host, port)
        app.run(host=host, port=port, threaded=True, use_reloader=False)
    except Exception:
        log.exception("Web server crashed — sampler unaffected.")
