"""Flask dashboard (SPECS §11): fake-press, relay status, live logs, restart.

Runs on its own thread inside the single process. Its failures must never take
down playback/relay logic — run_web_server swallows everything into the log.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

from flask import Flask, Response, jsonify, render_template, request
from werkzeug.utils import secure_filename

from .. import processing
from ..analytics import Analytics, NullAnalytics
from ..config import Config
from ..controller import Controller
from ..logging_setup import RingBufferHandler
from ..volume import MockVolume, VolumeControl, VolumeError

log = logging.getLogger(__name__)


def create_app(
    controller: Controller,
    ring: RingBufferHandler,
    restart_cb: Callable[[], None],
    cfg: Config,
    music_folder: Path,
    analytics: Analytics | None = None,
    volume: VolumeControl | None = None,
) -> Flask:
    analytics = analytics or NullAnalytics()
    # Default keeps existing call sites / tests valid without a real ALSA backend.
    volume = volume or MockVolume(floor=cfg.volume_min_pct)
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = cfg.upload_max_bytes
    token = cfg.web_auth_token
    extensions = {e.lower() for e in cfg.audio_extensions}

    if token:
        @app.before_request
        def _check_token():
            # The home-screen icon is fetched by iOS without our token query, so
            # static assets stay public (they expose nothing sensitive).
            if request.endpoint == "static":
                return
            supplied = request.args.get("token") or request.headers.get("X-Auth-Token")
            if supplied != token:
                return jsonify({"error": "Non autorisé."}), 401

    @app.get("/")
    def index():
        return render_template("analytics.html")

    @app.get("/debug")
    def debug_page():
        return render_template("debug.html")

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
        return jsonify({"ok": True,
                        "message": "Redémarrage — actualisez la page dans quelques secondes."})

    @app.get("/status")
    def status():
        return jsonify(controller.status())

    @app.get("/api/analytics")
    def api_analytics():
        return jsonify({"enabled": analytics.enabled,
                        **analytics.aggregates(cfg.analytics_top_n)})

    # ------------------------------------------------ volume (SPECS_PI_VOLUME)

    @app.get("/api/volume")
    def get_volume():
        if not cfg.volume_enabled:
            return jsonify({"volume": None, "enabled": False, "min": cfg.volume_min_pct})
        return jsonify({"volume": volume.get(), "enabled": True, "min": cfg.volume_min_pct})

    @app.post("/api/volume")
    def set_volume():
        if not cfg.volume_enabled:
            return jsonify({"error": "Le contrôle du volume est désactivé."}), 403
        data = request.get_json(silent=True) or {}
        raw = data.get("volume")
        try:
            pct = int(raw)
        except (TypeError, ValueError):
            return jsonify({"error": "volume manquant ou invalide."}), 400
        if not 0 <= pct <= 100:
            return jsonify({"error": "volume doit être entre 0 et 100."}), 400
        try:
            volume.set(pct)
        except VolumeError as e:
            log.error("Volume set to %d%% failed (%s): %s", pct, request.remote_addr, e)
            return jsonify({"error": "Contrôle du volume indisponible."}), 503
        applied = volume.get()
        log.info("Volume set to %d%% from web dashboard (%s).", pct, request.remote_addr)
        return jsonify({"ok": True, "volume": applied if applied is not None else pct})

    # ------------------------------------------------ song management (§11.1)

    def _valid_song_name(name: str) -> bool:
        # Bare filenames only — no separators, no "..", no dotfiles. Allows
        # spaces/accents so files copied in by other means stay manageable.
        if "/" in name or "\\" in name or ".." in name or name.startswith("."):
            return False
        return Path(name).suffix.lower() in extensions

    def _resolve_song(name: str) -> Path | None:
        target = (music_folder / name).resolve()
        if target.parent != music_folder.resolve() or not target.is_file():
            return None
        return target

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
        try:
            free_bytes = shutil.disk_usage(music_folder).free
        except OSError as e:
            log.error("Cannot read disk usage for %s: %s", music_folder, e)
            free_bytes = None
        return jsonify({
            "songs": songs,
            "extensions": sorted(extensions),
            "max_upload_bytes": cfg.upload_max_bytes,
            "clip_duration_s": cfg.clip_duration_s,
            "clip_min_s": cfg.clip_min_s,
            "clip_max_s": cfg.clip_max_s,
            "ffmpeg_available": processing.ffmpeg_available(),
            "free_bytes": free_bytes,
        })

    @app.post("/api/songs")
    def upload_song():
        if not processing.ffmpeg_available():
            return jsonify({"error": "ffmpeg n'est pas installé sur le serveur — "
                                     "les envois sont désactivés."}), 503
        f = request.files.get("file")
        if f is None or not f.filename:
            return jsonify({"error": "Aucun fichier fourni."}), 400
        try:
            start_s = float(request.form["start_s"])
        except (KeyError, ValueError):
            return jsonify({"error": "start_s manquant ou invalide."}), 400
        if not start_s >= 0:  # also rejects NaN
            return jsonify({"error": "start_s manquant ou invalide."}), 400
        # Clip length is chosen per upload; default to clip_duration_s when the
        # client omits it, reject garbage, then clamp to the configured window.
        if "duration_s" in request.form:
            try:
                duration_s = float(request.form["duration_s"])
            except ValueError:
                return jsonify({"error": "duration_s invalide."}), 400
            if not duration_s > 0:  # also rejects NaN
                return jsonify({"error": "duration_s invalide."}), 400
            duration_s = min(max(duration_s, cfg.clip_min_s), cfg.clip_max_s)
        else:
            duration_s = cfg.clip_duration_s
        name = secure_filename(f.filename)
        if not name or Path(name).suffix.lower() not in extensions:
            return jsonify({"error": "Seuls les fichiers %s sont acceptés."
                            % ", ".join(sorted(extensions))}), 400
        dest = music_folder / name
        if dest.exists():
            return jsonify({"error": f"{name} existe déjà — supprimez-le d'abord."}), 409
        # The raw upload lands under a unique .part name (invisible to the
        # startup scan, immune to concurrent-upload collisions); process_clip
        # writes dest atomically itself.
        fd, raw_name = tempfile.mkstemp(prefix=name + ".", suffix=".part",
                                        dir=str(music_folder))
        os.close(fd)
        raw = Path(raw_name)
        try:
            f.save(raw)
            processing.process_clip(raw, dest, start_s=start_s,
                                    clip_s=duration_s,
                                    target_lufs=cfg.loudness_target_lufs)
            size = dest.stat().st_size
        except processing.ProcessingError as e:
            log.error("Upload of %s failed: %s", name, e)
            return jsonify({"error": f"Impossible de traiter l'audio : {e}"}), 500
        except OSError as e:
            log.error("Upload of %s failed: %s", name, e)
            return jsonify({"error": "Impossible d'enregistrer le fichier."}), 500
        finally:
            raw.unlink(missing_ok=True)
        log.info("Uploaded %s: %.0f s clip from %.1f s, normalized to %.1f LUFS "
                 "(%d bytes) from web dashboard (%s) — restart to apply.",
                 name, duration_s, start_s, cfg.loudness_target_lufs,
                 size, request.remote_addr)
        return jsonify({"ok": True, "name": name, "size_bytes": size}), 201

    @app.post("/api/songs/<name>/normalize")
    def normalize_song(name: str):
        if not processing.ffmpeg_available():
            return jsonify({"error": "ffmpeg n'est pas installé sur le serveur — "
                                     "la normalisation est désactivée."}), 503
        if not _valid_song_name(name):
            return jsonify({"error": "Nom invalide."}), 400
        target = _resolve_song(name)
        if target is None:
            return jsonify({"error": "Chanson introuvable."}), 404
        try:
            processing.normalize_in_place(target, cfg.loudness_target_lufs)
        except processing.ProcessingError as e:
            log.error("Normalize of %s failed: %s", name, e)
            return jsonify({"error": f"Impossible de normaliser : {e}"}), 500
        size = target.stat().st_size
        # Same filename, so no restart needed: tracks are opened at play time
        log.info("Normalized %s to %.1f LUFS (%d bytes) from web dashboard (%s).",
                 name, cfg.loudness_target_lufs, size, request.remote_addr)
        return jsonify({"ok": True, "name": name, "size_bytes": size})

    @app.delete("/api/songs/<name>")
    def delete_song(name: str):
        if not _valid_song_name(name):
            return jsonify({"error": "Nom invalide."}), 400
        target = _resolve_song(name)
        if target is None:
            return jsonify({"error": "Chanson introuvable."}), 404
        try:
            target.unlink()
        except OSError as e:
            log.error("Delete of %s failed: %s", name, e)
            return jsonify({"error": "Impossible de supprimer le fichier."}), 500
        log.info("Deleted %s from web dashboard (%s) — restart to apply.",
                 name, request.remote_addr)
        return jsonify({"ok": True})

    @app.errorhandler(413)
    def too_large(_e):
        return jsonify({"error": "Le fichier dépasse la limite d'envoi de %.1f Mo."
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
