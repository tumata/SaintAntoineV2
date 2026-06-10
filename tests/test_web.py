"""Web dashboard endpoints against a fully mocked controller."""

import io

import pytest

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


# --------------------------------------------------- song management (§11.1)


def _upload(client, filename, data=b"\x00\x01"):
    return client.post("/api/songs",
                       data={"file": (io.BytesIO(data), filename)},
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


def test_upload_song(web):
    client, harness, _, _ = web
    response = _upload(client, "new song.mp3", b"abc")
    assert response.status_code == 201
    assert response.get_json()["name"] == "new_song.mp3"  # secure_filename applied
    saved = harness.music_folder / "new_song.mp3"
    assert saved.read_bytes() == b"abc"
    assert not list(harness.music_folder.glob("*.part"))


def test_upload_rejects_bad_extension(web):
    client, harness, _, _ = web
    assert _upload(client, "evil.txt").status_code == 400
    assert _upload(client, "noext").status_code == 400
    assert not (harness.music_folder / "evil.txt").exists()


def test_upload_rejects_collision(web):
    client, harness, _, _ = web
    response = _upload(client, "song0.mp3", b"overwritten")
    assert response.status_code == 409
    assert (harness.music_folder / "song0.mp3").read_bytes() == b"\x00"


def test_upload_rejects_oversize(make_harness):
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
