"""Web dashboard endpoints against a fully mocked controller."""

import io
import os
from pathlib import Path

import pytest

from saintantoine import processing, youtube
from saintantoine.analytics import SqliteAnalytics
from saintantoine.controller import PLAYING
from saintantoine.logging_setup import RingBufferHandler
from saintantoine.volume import MockVolume
from saintantoine.web.server import create_app


@pytest.fixture
def web(tmp_path, make_harness):
    analytics = SqliteAnalytics(tmp_path / "analytics.db")
    harness = make_harness(analytics=analytics)
    ring = RingBufferHandler(100)
    restarts = []
    volume = MockVolume(initial=60, floor=harness.cfg.volume_min_pct)
    app = create_app(harness.controller, ring, lambda: restarts.append(True),
                     harness.cfg, harness.music_folder, analytics=analytics,
                     volume=volume)
    app.config["TESTING"] = True
    return app.test_client(), harness, restarts, ring, volume


def test_status(web):
    client, harness, _, _, _ = web
    response = client.get("/status")
    assert response.status_code == 200
    data = response.get_json()
    assert data["state"] == "IDLE"
    assert data["relays"] == [False, False, False]
    assert data["mode"] == "mock"


def test_press_triggers_controller(web):
    client, harness, _, _, _ = web
    response = client.post("/press")
    assert response.status_code == 200
    assert response.get_json()["status"]["state"] == PLAYING
    assert harness.controller.state == PLAYING
    assert harness.gpio.relay_states() == [True, True, True]


def test_restart_invokes_callback(web):
    client, _, restarts, _, _ = web
    response = client.post("/restart")
    assert response.status_code == 200
    import time

    deadline = time.time() + 3
    while not restarts and time.time() < deadline:
        time.sleep(0.05)
    assert restarts == [True]


def test_landing_is_analytics_page(web):
    client, _, _, _, _ = web
    response = client.get("/")
    assert response.status_code == 200
    assert b"Statistiques" in response.data
    assert b"/static/chart.min.js" in response.data


def test_get_volume(web):
    client, _, _, _, volume = web
    response = client.get("/api/volume")
    assert response.status_code == 200
    data = response.get_json()
    assert data == {"volume": 60, "enabled": True, "min": volume.floor}


def test_set_volume(web):
    client, _, _, _, volume = web
    response = client.post("/api/volume", json={"volume": 80})
    assert response.status_code == 200
    assert response.get_json()["volume"] == 80
    assert volume.get() == 80


def test_set_volume_clamps_to_floor(web):
    # Below the floor is accepted by the API but clamped on apply (play-louder intent).
    client, _, _, _, volume = web
    response = client.post("/api/volume", json={"volume": 10})
    assert response.status_code == 200
    assert response.get_json()["volume"] == volume.floor
    assert volume.get() == volume.floor


def test_set_volume_rejects_out_of_range(web):
    client, _, _, _, volume = web
    for bad in (-5, 101, 999):
        response = client.post("/api/volume", json={"volume": bad})
        assert response.status_code == 400


def test_set_volume_rejects_invalid(web):
    client, _, _, _, _ = web
    assert client.post("/api/volume", json={}).status_code == 400
    assert client.post("/api/volume", json={"volume": "loud"}).status_code == 400
    assert client.post("/api/volume", data="not json",
                       content_type="application/json").status_code == 400


def test_debug_page(web):
    client, _, _, _, _ = web
    response = client.get("/debug")
    assert response.status_code == 200
    assert "Appuyer".encode() in response.data
    assert "Redémarrer".encode() in response.data


def test_analytics_endpoint_shape(web):
    client, _, _, _, _ = web
    data = client.get("/api/analytics").get_json()
    assert data["enabled"] is True
    assert len(data["by_hour"]) == 24
    assert data["top_tracks"] == []
    assert data["total_plays"] == 0


def test_press_records_in_analytics(web):
    client, _, _, _, _ = web
    client.post("/press")
    data = client.get("/api/analytics").get_json()
    assert data["total_plays"] == 1
    assert client.get("/status").get_json()["analytics_rev"] == 1


def test_logs_endpoint(web):
    client, _, _, ring, _ = web
    import logging

    record = logging.LogRecord("test", logging.INFO, __file__, 1, "hello ring", (), None)
    ring.emit(record)
    data = client.get("/logs?since=0").get_json()
    assert any("hello ring" in e["line"] for e in data["entries"])
    assert data["latest"] >= 1


def test_token_required_when_configured(tmp_path, make_harness):
    harness = make_harness()
    harness.cfg.web_auth_token = "sekret"
    ring = RingBufferHandler(10)
    app = create_app(harness.controller, ring, lambda: None, harness.cfg,
                     harness.music_folder)
    app.config["TESTING"] = True
    client = app.test_client()

    assert client.get("/status").status_code == 401
    assert client.post("/press").status_code == 401
    assert client.get("/status?token=sekret").status_code == 200
    assert client.get("/status", headers={"X-Auth-Token": "sekret"}).status_code == 200
    assert client.get("/api/songs").status_code == 401
    assert client.get("/api/songs?token=sekret").status_code == 200


# --------------------------------------------- song management (§11.1, §11.2)


@pytest.fixture
def fake_processing(monkeypatch):
    """Pretend ffmpeg exists; record processing calls instead of running it.

    Keeps tests independent of whether ffmpeg is installed on the machine.
    """
    calls = {"clips": [], "normalized": []}
    monkeypatch.setattr(processing, "ffmpeg_available", lambda: True)

    def fake_clip(src, dest, start_s, clip_s, target_lufs):
        calls["clips"].append((src.name, dest.name, start_s, clip_s, target_lufs))
        dest.write_bytes(b"clip:" + src.read_bytes())

    def fake_norm(path, target_lufs):
        calls["normalized"].append((path.name, target_lufs))
        path.write_bytes(b"norm:" + path.read_bytes())

    monkeypatch.setattr(processing, "process_clip", fake_clip)
    monkeypatch.setattr(processing, "normalize_in_place", fake_norm)
    return calls


def _upload(client, filename, data=b"\x00\x01", start_s="3.5", duration_s=None):
    payload = {"file": (io.BytesIO(data), filename)}
    if start_s is not None:
        payload["start_s"] = start_s
    if duration_s is not None:
        payload["duration_s"] = duration_s
    return client.post("/api/songs", data=payload,
                       content_type="multipart/form-data")


def test_songs_page(web):
    client, _, _, _, _ = web
    response = client.get("/songs")
    assert response.status_code == 200
    assert "Ajouter une chanson".encode() in response.data


def test_list_songs(web):
    client, harness, _, _, _ = web
    # Newest first (by mtime): song2 added last, song0 first.
    for i in range(3):
        os.utime(harness.music_folder / f"song{i}.mp3", (1000 + i, 1000 + i))
    data = client.get("/api/songs").get_json()
    assert [s["name"] for s in data["songs"]] == ["song2.mp3", "song1.mp3", "song0.mp3"]
    assert all(s["size_bytes"] == 1 for s in data["songs"])
    assert data["max_upload_bytes"] == harness.cfg.upload_max_bytes
    assert ".mp3" in data["extensions"]
    assert data["clip_duration_s"] == harness.cfg.clip_duration_s
    assert data["clip_min_s"] == harness.cfg.clip_min_s
    assert data["clip_max_s"] == harness.cfg.clip_max_s
    assert isinstance(data["ffmpeg_available"], bool)
    assert isinstance(data["free_bytes"], int) and data["free_bytes"] > 0


def test_upload_song(web, fake_processing):
    client, harness, _, _, _ = web
    response = _upload(client, "new song.mp3", b"abc", start_s="42.5")
    assert response.status_code == 201
    assert response.get_json()["name"] == "new_song.mp3"  # secure_filename applied
    saved = harness.music_folder / "new_song.mp3"
    assert saved.read_bytes() == b"clip:abc"  # went through process_clip
    [(src, dest, start, clip, target)] = fake_processing["clips"]
    assert src.startswith("new_song.mp3.") and src.endswith(".part")  # raw temp
    assert (dest, start, clip, target) == ("new_song.mp3", 42.5,
                                           harness.cfg.clip_duration_s,
                                           harness.cfg.loudness_target_lufs)
    assert not list(harness.music_folder.glob("*.part"))  # raw temp cleaned up


def test_upload_requires_valid_start(web, fake_processing):
    client, harness, _, _, _ = web
    assert _upload(client, "a.mp3", start_s=None).status_code == 400
    assert _upload(client, "a.mp3", start_s="abc").status_code == 400
    assert _upload(client, "a.mp3", start_s="-1").status_code == 400
    assert _upload(client, "a.mp3", start_s="nan").status_code == 400
    assert not (harness.music_folder / "a.mp3").exists()
    assert fake_processing["clips"] == []


def test_upload_honors_duration(web, fake_processing):
    client, harness, _, _, _ = web
    mid = (harness.cfg.clip_min_s + harness.cfg.clip_max_s) / 2
    assert _upload(client, "a.mp3", duration_s=str(mid)).status_code == 201
    [(_, _, _, clip, _)] = fake_processing["clips"]
    assert clip == mid


def test_upload_clamps_duration_to_bounds(web, fake_processing):
    client, harness, _, _, _ = web
    lo, hi = harness.cfg.clip_min_s, harness.cfg.clip_max_s
    assert _upload(client, "low.mp3", duration_s=str(lo - 5)).status_code == 201
    assert _upload(client, "high.mp3", duration_s=str(hi + 99)).status_code == 201
    clips = sorted(c[3] for c in fake_processing["clips"])
    assert clips == [lo, hi]


def test_upload_rejects_invalid_duration(web, fake_processing):
    client, harness, _, _, _ = web
    assert _upload(client, "a.mp3", duration_s="abc").status_code == 400
    assert _upload(client, "a.mp3", duration_s="0").status_code == 400
    assert _upload(client, "a.mp3", duration_s="-5").status_code == 400
    assert _upload(client, "a.mp3", duration_s="nan").status_code == 400
    assert fake_processing["clips"] == []


def test_upload_503_without_ffmpeg(web, monkeypatch):
    client, harness, _, _, _ = web
    monkeypatch.setattr(processing, "ffmpeg_available", lambda: False)
    response = _upload(client, "a.mp3")
    assert response.status_code == 503
    assert "ffmpeg" in response.get_json()["error"]
    assert not (harness.music_folder / "a.mp3").exists()


def test_upload_processing_failure_cleans_up(web, monkeypatch):
    client, harness, _, _, _ = web
    monkeypatch.setattr(processing, "ffmpeg_available", lambda: True)

    def boom(*args, **kwargs):
        raise processing.ProcessingError("clip failed")

    monkeypatch.setattr(processing, "process_clip", boom)
    response = _upload(client, "a.mp3")
    assert response.status_code == 500
    assert "clip failed" in response.get_json()["error"]
    assert not (harness.music_folder / "a.mp3").exists()
    assert not list(harness.music_folder.glob("*.part"))


def test_upload_rejects_bad_extension(web, fake_processing):
    client, harness, _, _, _ = web
    assert _upload(client, "evil.txt").status_code == 400
    assert _upload(client, "noext").status_code == 400
    assert not (harness.music_folder / "evil.txt").exists()


def test_upload_rejects_collision(web, fake_processing):
    client, harness, _, _, _ = web
    response = _upload(client, "song0.mp3", b"overwritten")
    assert response.status_code == 409
    assert (harness.music_folder / "song0.mp3").read_bytes() == b"\x00"


def test_upload_rejects_oversize(make_harness, fake_processing):
    harness = make_harness()
    harness.cfg.upload_max_bytes = 10
    app = create_app(harness.controller, RingBufferHandler(10), lambda: None,
                     harness.cfg, harness.music_folder)
    app.config["TESTING"] = True
    client = app.test_client()
    response = _upload(client, "big.mp3", b"x" * 1000)
    assert response.status_code == 413
    assert "limit" in response.get_json()["error"]
    assert not (harness.music_folder / "big.mp3").exists()


def test_normalize_song(web, fake_processing):
    client, harness, _, _, _ = web
    response = client.post("/api/songs/song1.mp3/normalize")
    assert response.status_code == 200
    data = response.get_json()
    assert data["name"] == "song1.mp3"
    assert (harness.music_folder / "song1.mp3").read_bytes() == b"norm:\x00"
    assert data["size_bytes"] == 6
    assert fake_processing["normalized"] == [
        ("song1.mp3", harness.cfg.loudness_target_lufs)]


def test_normalize_503_without_ffmpeg(web, monkeypatch):
    client, _, _, _, _ = web
    monkeypatch.setattr(processing, "ffmpeg_available", lambda: False)
    assert client.post("/api/songs/song1.mp3/normalize").status_code == 503


def test_normalize_rejects_bad_names(web, fake_processing):
    client, harness, _, _, _ = web
    (harness.music_folder / "notes.txt").write_text("keep me")
    assert client.post("/api/songs/ghost.mp3/normalize").status_code == 404
    assert client.post("/api/songs/..secret.mp3/normalize").status_code == 400
    assert client.post("/api/songs/notes.txt/normalize").status_code == 400
    assert fake_processing["normalized"] == []


def test_normalize_failure_keeps_file(web, monkeypatch):
    client, harness, _, _, _ = web
    monkeypatch.setattr(processing, "ffmpeg_available", lambda: True)

    def boom(*args, **kwargs):
        raise processing.ProcessingError("normalize failed")

    monkeypatch.setattr(processing, "normalize_in_place", boom)
    response = client.post("/api/songs/song1.mp3/normalize")
    assert response.status_code == 500
    assert (harness.music_folder / "song1.mp3").read_bytes() == b"\x00"


def test_delete_song(web):
    client, harness, _, _, _ = web
    for i in range(3):
        os.utime(harness.music_folder / f"song{i}.mp3", (1000 + i, 1000 + i))
    response = client.delete("/api/songs/song1.mp3")
    assert response.status_code == 200
    assert not (harness.music_folder / "song1.mp3").exists()
    names = [s["name"] for s in client.get("/api/songs").get_json()["songs"]]
    assert names == ["song2.mp3", "song0.mp3"]  # newest first


def test_delete_missing_song(web):
    client, _, _, _, _ = web
    assert client.delete("/api/songs/ghost.mp3").status_code == 404


def test_delete_rejects_traversal_and_non_audio(web):
    client, harness, _, _, _ = web
    (harness.music_folder / "notes.txt").write_text("keep me")
    secret = harness.music_folder.parent / "secret.mp3"
    secret.write_bytes(b"\x00")

    assert client.delete("/api/songs/..secret.mp3").status_code == 400
    assert client.delete("/api/songs/notes.txt").status_code == 400
    assert client.delete("/api/songs/%2e%2e%2fsecret.mp3").status_code in (400, 404)
    assert secret.exists()
    assert (harness.music_folder / "notes.txt").exists()


# ------------------------------------------ YouTube import (SPECS_YOUTUBE_IMPORT)


VALID_URL = "https://youtu.be/dQw4w9WgXcQ"


@pytest.fixture
def fake_youtube(monkeypatch):
    """Pretend yt-dlp is installed; write a fake MP3 instead of downloading.

    Keeps tests independent of yt-dlp and the network. Returns the recorded
    download calls so tests can assert on the args.
    """
    calls = []
    monkeypatch.setattr(youtube, "youtube_available", lambda: True)

    def fake_download(url, dest_dir, *, max_duration_s, max_filesize, timeout_s):
        calls.append((url, max_duration_s, max_filesize, timeout_s))
        path = Path(dest_dir) / "Rick Astley - Never Gonna Give You Up.mp3"
        path.write_bytes(b"ID3fake-mp3-bytes")
        return path, path.stem

    monkeypatch.setattr(youtube, "download_audio", fake_download)
    return calls


def test_songs_lists_youtube_flags(web):
    client, _, _, _, _ = web
    data = client.get("/api/songs").get_json()
    assert data["youtube_enabled"] is True
    assert isinstance(data["youtube_available"], bool)


def test_youtube_success(web, fake_youtube):
    client, _, _, _, _ = web
    response = client.post("/api/youtube", json={"url": VALID_URL})
    assert response.status_code == 200
    assert response.mimetype == "audio/mpeg"
    assert response.data == b"ID3fake-mp3-bytes"
    # Title round-trips percent-encoded so accents/spaces survive the header.
    from urllib.parse import unquote
    assert unquote(response.headers["X-Song-Title"]) == \
        "Rick Astley - Never Gonna Give You Up"
    # Caps from config are passed through to the downloader.
    assert fake_youtube[0][1] == 900 and fake_youtube[0][2] == "50M"


def test_youtube_disabled(web, fake_youtube):
    client, harness, _, _, _ = web
    harness.cfg.youtube_enabled = False
    assert client.post("/api/youtube", json={"url": VALID_URL}).status_code == 403


def test_youtube_unavailable(web, monkeypatch):
    client, _, _, _, _ = web
    monkeypatch.setattr(youtube, "youtube_available", lambda: False)
    assert client.post("/api/youtube", json={"url": VALID_URL}).status_code == 503


def test_youtube_rejects_non_youtube_url(web, fake_youtube):
    client, _, _, _, _ = web
    assert client.post("/api/youtube", json={"url": "https://evil.example/x"}).status_code == 400
    assert client.post("/api/youtube", json={"url": "not a url"}).status_code == 400
    assert client.post("/api/youtube", json={}).status_code == 400
    # A rejected URL must never reach the downloader.
    assert fake_youtube == []


def test_youtube_download_failure(web, monkeypatch):
    client, _, _, _, _ = web
    monkeypatch.setattr(youtube, "youtube_available", lambda: True)

    def boom(*a, **k):
        raise youtube.YoutubeError("yt-dlp exited with code 1")

    monkeypatch.setattr(youtube, "download_audio", boom)
    assert client.post("/api/youtube", json={"url": VALID_URL}).status_code == 502


def test_youtube_busy_returns_429(web, fake_youtube):
    client, _, _, _, _ = web
    assert youtube.IMPORT_LOCK.acquire(blocking=False)  # simulate an in-flight import
    try:
        assert client.post("/api/youtube", json={"url": VALID_URL}).status_code == 429
    finally:
        youtube.IMPORT_LOCK.release()
    # Lock is released after a normal request, so the next import succeeds.
    assert client.post("/api/youtube", json={"url": VALID_URL}).status_code == 200
