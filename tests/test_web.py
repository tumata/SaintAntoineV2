"""Web dashboard endpoints against a fully mocked controller."""

import io

import pytest

from saintantoine import processing
from saintantoine.controller import PLAYING
from saintantoine.logging_setup import RingBufferHandler
from saintantoine.web.server import create_app


@pytest.fixture
def web(harness):
    ring = RingBufferHandler(100)
    restarts = []
    app = create_app(harness.controller, ring, lambda: restarts.append(True),
                     harness.cfg, harness.music_folder)
    app.config["TESTING"] = True
    return app.test_client(), harness, restarts, ring


def test_status(web):
    client, harness, _, _ = web
    response = client.get("/status")
    assert response.status_code == 200
    data = response.get_json()
    assert data["state"] == "IDLE"
    assert data["relays"] == [False, False, False]
    assert data["mode"] == "mock"


def test_press_triggers_controller(web):
    client, harness, _, _ = web
    response = client.post("/press")
    assert response.status_code == 200
    assert response.get_json()["status"]["state"] == PLAYING
    assert harness.controller.state == PLAYING
    assert harness.gpio.relay_states() == [True, True, True]


def test_restart_invokes_callback(web):
    client, _, restarts, _ = web
    response = client.post("/restart")
    assert response.status_code == 200
    import time

    deadline = time.time() + 3
    while not restarts and time.time() < deadline:
        time.sleep(0.05)
    assert restarts == [True]


def test_dashboard_page(web):
    client, _, _, _ = web
    response = client.get("/")
    assert response.status_code == 200
    assert b"Saint Antoine" in response.data


def test_logs_endpoint(web):
    client, _, _, ring = web
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


def _upload(client, filename, data=b"\x00\x01", start_s="3.5"):
    payload = {"file": (io.BytesIO(data), filename)}
    if start_s is not None:
        payload["start_s"] = start_s
    return client.post("/api/songs", data=payload,
                       content_type="multipart/form-data")


def test_songs_page(web):
    client, _, _, _ = web
    response = client.get("/songs")
    assert response.status_code == 200
    assert b"Upload song" in response.data


def test_list_songs(web):
    client, harness, _, _ = web
    data = client.get("/api/songs").get_json()
    assert [s["name"] for s in data["songs"]] == ["song0.mp3", "song1.mp3", "song2.mp3"]
    assert all(s["size_bytes"] == 1 for s in data["songs"])
    assert data["max_upload_bytes"] == harness.cfg.upload_max_bytes
    assert ".mp3" in data["extensions"]
    assert data["clip_duration_s"] == harness.cfg.clip_duration_s
    assert isinstance(data["ffmpeg_available"], bool)


def test_upload_song(web, fake_processing):
    client, harness, _, _ = web
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
    client, harness, _, _ = web
    assert _upload(client, "a.mp3", start_s=None).status_code == 400
    assert _upload(client, "a.mp3", start_s="abc").status_code == 400
    assert _upload(client, "a.mp3", start_s="-1").status_code == 400
    assert _upload(client, "a.mp3", start_s="nan").status_code == 400
    assert not (harness.music_folder / "a.mp3").exists()
    assert fake_processing["clips"] == []


def test_upload_503_without_ffmpeg(web, monkeypatch):
    client, harness, _, _ = web
    monkeypatch.setattr(processing, "ffmpeg_available", lambda: False)
    response = _upload(client, "a.mp3")
    assert response.status_code == 503
    assert "ffmpeg" in response.get_json()["error"]
    assert not (harness.music_folder / "a.mp3").exists()


def test_upload_processing_failure_cleans_up(web, monkeypatch):
    client, harness, _, _ = web
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
    client, harness, _, _ = web
    assert _upload(client, "evil.txt").status_code == 400
    assert _upload(client, "noext").status_code == 400
    assert not (harness.music_folder / "evil.txt").exists()


def test_upload_rejects_collision(web, fake_processing):
    client, harness, _, _ = web
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
    client, harness, _, _ = web
    response = client.post("/api/songs/song1.mp3/normalize")
    assert response.status_code == 200
    data = response.get_json()
    assert data["name"] == "song1.mp3"
    assert (harness.music_folder / "song1.mp3").read_bytes() == b"norm:\x00"
    assert data["size_bytes"] == 6
    assert fake_processing["normalized"] == [
        ("song1.mp3", harness.cfg.loudness_target_lufs)]


def test_normalize_503_without_ffmpeg(web, monkeypatch):
    client, _, _, _ = web
    monkeypatch.setattr(processing, "ffmpeg_available", lambda: False)
    assert client.post("/api/songs/song1.mp3/normalize").status_code == 503


def test_normalize_rejects_bad_names(web, fake_processing):
    client, harness, _, _ = web
    (harness.music_folder / "notes.txt").write_text("keep me")
    assert client.post("/api/songs/ghost.mp3/normalize").status_code == 404
    assert client.post("/api/songs/..secret.mp3/normalize").status_code == 400
    assert client.post("/api/songs/notes.txt/normalize").status_code == 400
    assert fake_processing["normalized"] == []


def test_normalize_failure_keeps_file(web, monkeypatch):
    client, harness, _, _ = web
    monkeypatch.setattr(processing, "ffmpeg_available", lambda: True)

    def boom(*args, **kwargs):
        raise processing.ProcessingError("normalize failed")

    monkeypatch.setattr(processing, "normalize_in_place", boom)
    response = client.post("/api/songs/song1.mp3/normalize")
    assert response.status_code == 500
    assert (harness.music_folder / "song1.mp3").read_bytes() == b"\x00"


def test_delete_song(web):
    client, harness, _, _ = web
    response = client.delete("/api/songs/song1.mp3")
    assert response.status_code == 200
    assert not (harness.music_folder / "song1.mp3").exists()
    names = [s["name"] for s in client.get("/api/songs").get_json()["songs"]]
    assert names == ["song0.mp3", "song2.mp3"]


def test_delete_missing_song(web):
    client, _, _, _ = web
    assert client.delete("/api/songs/ghost.mp3").status_code == 404


def test_delete_rejects_traversal_and_non_audio(web):
    client, harness, _, _ = web
    (harness.music_folder / "notes.txt").write_text("keep me")
    secret = harness.music_folder.parent / "secret.mp3"
    secret.write_bytes(b"\x00")

    assert client.delete("/api/songs/..secret.mp3").status_code == 400
    assert client.delete("/api/songs/notes.txt").status_code == 400
    assert client.delete("/api/songs/%2e%2e%2fsecret.mp3").status_code in (400, 404)
    assert secret.exists()
    assert (harness.music_folder / "notes.txt").exists()
