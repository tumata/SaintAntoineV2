"""Webhook: disabled without URL, correct payload, failures tolerated."""

import io
import json

import saintantoine.webhook as webhook_mod
from saintantoine.webhook import Webhook


class FakeResponse(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_disabled_without_url():
    hook = Webhook("")
    assert hook.enabled is False
    assert hook.fire("song.mp3") is None


def test_payload_shape(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        captured["content_type"] = request.get_header("Content-type")
        return FakeResponse()

    monkeypatch.setattr(webhook_mod.urllib.request, "urlopen", fake_urlopen)

    hook = Webhook("https://example.com/hook", timeout_s=2.0)
    thread = hook.fire("song.mp3")
    thread.join(timeout=5)

    assert captured["url"] == "https://example.com/hook"
    assert captured["body"]["track"] == "song.mp3"
    assert "timestamp" in captured["body"]
    assert captured["timeout"] == 2.0
    assert captured["content_type"] == "application/json"


def test_failure_tolerated(monkeypatch):
    def fake_urlopen(request, timeout=None):
        raise OSError("network down")

    monkeypatch.setattr(webhook_mod.urllib.request, "urlopen", fake_urlopen)

    hook = Webhook("https://example.com/hook")
    thread = hook.fire("song.mp3")
    thread.join(timeout=5)  # must not raise, must not hang
