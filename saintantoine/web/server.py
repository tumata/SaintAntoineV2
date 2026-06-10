"""Flask dashboard (SPECS §11): fake-press, relay status, live logs, restart.

Runs on its own thread inside the single process. Its failures must never take
down playback/relay logic — run_web_server swallows everything into the log.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Callable

from flask import Flask, Response, jsonify, render_template, request
from werkzeug.utils import secure_filename

from ..config import Config
from ..controller import Controller
from ..logging_setup import RingBufferHandler

log = logging.getLogger(__name__)


def create_app(
    controller: Controller,
    ring: RingBufferHandler,
    restart_cb: Callable[[], None],
    cfg: Config,
    music_folder: Path,
) -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = cfg.upload_max_bytes
    token = cfg.web_auth_token
    extensions = {e.lower() for e in cfg.audio_extensions}

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

    # ------------------------------------------------ song management (§11.1)

    @app.get("/songs")
    def songs_page():
        return render_template("songs.html")

    @app.get("/api/songs")
    def list_songs():
        songs = []
        try:
            for p in sorted(music_folder.iterdir()):
                if p.is_file() and p.suffix.lower() in extensions:
                    songs.append({"name": p.name, "size_bytes": p.stat().st_size})
        except OSError as e:
            log.error("Cannot read music folder %s: %s", music_folder, e)
        return jsonify({
            "songs": songs,
            "extensions": sorted(extensions),
            "max_upload_bytes": cfg.upload_max_bytes,
        })

    @app.post("/api/songs")
    def upload_song():
        f = request.files.get("file")
        if f is None or not f.filename:
            return jsonify({"error": "No file provided."}), 400
        name = secure_filename(f.filename)
        if not name or Path(name).suffix.lower() not in extensions:
            return jsonify({"error": "Only %s files are accepted."
                            % ", ".join(sorted(extensions))}), 400
        dest = music_folder / name
        if dest.exists():
            return jsonify({"error": f"{name} already exists — delete it first."}), 409
        # Atomic write: never let a startup scan see a half-uploaded file
        tmp = music_folder / (name + ".part")
        try:
            f.save(tmp)
            size = tmp.stat().st_size
            tmp.rename(dest)
        except OSError as e:
            tmp.unlink(missing_ok=True)
            log.error("Upload of %s failed: %s", name, e)
            return jsonify({"error": "Could not save file."}), 500
        log.info("Uploaded %s (%d bytes) from web dashboard (%s) — restart to apply.",
                 name, size, request.remote_addr)
        return jsonify({"ok": True, "name": name, "size_bytes": size}), 201

    @app.delete("/api/songs/<name>")
    def delete_song(name: str):
        # Bare filenames only — no separators, no "..", no dotfiles. Allows
        # spaces/accents so files copied in by other means stay deletable.
        if "/" in name or "\\" in name or ".." in name or name.startswith("."):
            return jsonify({"error": "Invalid name."}), 400
        if Path(name).suffix.lower() not in extensions:
            return jsonify({"error": "Invalid name."}), 400
        target = (music_folder / name).resolve()
        if target.parent != music_folder.resolve() or not target.is_file():
            return jsonify({"error": "No such song."}), 404
        try:
            target.unlink()
        except OSError as e:
            log.error("Delete of %s failed: %s", name, e)
            return jsonify({"error": "Could not delete file."}), 500
        log.info("Deleted %s from web dashboard (%s) — restart to apply.",
                 name, request.remote_addr)
        return jsonify({"ok": True})

    @app.errorhandler(413)
    def too_large(_e):
        return jsonify({"error": "File exceeds the %.1f MB upload limit."
                        % (cfg.upload_max_bytes / 1_000_000)}), 413

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
