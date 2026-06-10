"""Flask dashboard (SPECS §11): fake-press, relay status, live logs, restart.

Runs on its own thread inside the single process. Its failures must never take
down playback/relay logic — run_web_server swallows everything into the log.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Callable

from flask import Flask, Response, jsonify, render_template, request

from ..config import Config
from ..controller import Controller
from ..logging_setup import RingBufferHandler

log = logging.getLogger(__name__)


def create_app(
    controller: Controller,
    ring: RingBufferHandler,
    restart_cb: Callable[[], None],
    cfg: Config,
) -> Flask:
    app = Flask(__name__)
    token = cfg.web_auth_token

    if token:
        @app.before_request
        def _check_token():
            supplied = request.args.get("token") or request.headers.get("X-Auth-Token")
            if supplied != token:
                return jsonify({"error": "unauthorized"}), 401

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.post("/press")
    def press():
        log.info("Fake press from web dashboard (%s).", request.remote_addr)
        controller.on_press()
        return jsonify({"ok": True, "status": controller.status()})

    @app.post("/restart")
    def restart():
        log.warning("Restart requested from web dashboard (%s).", request.remote_addr)
        # Small delay so this response reaches the browser before the process exits
        threading.Timer(0.3, restart_cb).start()
        return jsonify({"ok": True, "message": "Restarting — refresh the page in a few seconds."})

    @app.get("/status")
    def status():
        return jsonify(controller.status())

    @app.get("/logs")
    def logs():
        since = request.args.get("since", 0, type=int)
        entries = ring.since(since)
        return jsonify({
            "entries": [{"seq": seq, "line": line} for seq, line in entries],
            "latest": ring.latest_seq(),
        })

    @app.get("/events")
    def events():
        def generate():
            last_seq = ring.latest_seq() - 50  # replay recent history on connect
            while True:
                for seq, line in ring.since(last_seq):
                    last_seq = seq
                    yield f"id: {seq}\nevent: log\ndata: {line}\n\n"
                yield f"event: status\ndata: {json.dumps(controller.status())}\n\n"
                time.sleep(1.0)

        return Response(generate(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return app


def run_web_server(app: Flask, cfg: Config) -> None:
    """Thread target. Never lets a web failure propagate to the rest of the process."""
    try:
        log.info("Web dashboard on http://%s:%d (reachable over Tailscale).",
                 cfg.web_host, cfg.web_port)
        app.run(host=cfg.web_host, port=cfg.web_port, threaded=True, use_reloader=False)
    except Exception:
        log.exception("Web server crashed — playback/relays unaffected.")
