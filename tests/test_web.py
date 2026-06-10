"""Web dashboard endpoints against a fully mocked controller."""

import pytest

from saintantoine.controller import PLAYING
from saintantoine.logging_setup import RingBufferHandler
from saintantoine.web.server import create_app


@pytest.fixture
def web(harness):
    ring = RingBufferHandler(100)
    restarts = []
    app = create_app(harness.controller, ring, lambda: restarts.append(True), harness.cfg)
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
    app = create_app(harness.controller, ring, lambda: None, harness.cfg)
    app.config["TESTING"] = True
    client = app.test_client()

    assert client.get("/status").status_code == 401
    assert client.post("/press").status_code == 401
    assert client.get("/status?token=sekret").status_code == 200
    assert client.get("/status", headers={"X-Auth-Token": "sekret"}).status_code == 200
