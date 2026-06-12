"""ButtonScope tests (SPECS_BUTTONSCOPE §10): edge detection, ring buffer,
glitch classification, mock signal, live pin swap, web endpoints."""

import pytest

from buttonscope.sampler import InputSource, MockSignal, Sampler
from buttonscope.web import create_app, sse_payload


class ScriptedSource(InputSource):
    """Level is a pure function of time; records close() calls."""

    def __init__(self, fn, pin=22, pull="down"):
        self.fn = fn
        self.pin = pin
        self.pull = pull
        self.closed = False

    def read(self, now):
        return self.fn(now)

    def close(self):
        self.closed = True


def make_sampler(fn, pull="down", glitch_ms=20.0, retention_s=15.0):
    sources = []

    def factory(pin, p):
        src = ScriptedSource(fn, pin, p)
        sources.append(src)
        return src

    sampler = Sampler(factory, 22, pull, glitch_ms=glitch_ms, retention_s=retention_s)
    return sampler, sources


def run_ticks(sampler, t_from, t_to, step=0.001):
    t = t_from
    while t < t_to:
        sampler.tick(t)
        t += step


def pulse(start, end):
    return lambda now: start <= now < end


# ------------------------------------------------------------ edge detection


def test_edge_detection_and_timestamps():
    sampler, _ = make_sampler(pulse(0.100, 0.200))
    run_ticks(sampler, 0.0, 0.3)
    snap = sampler.snapshot_since(0)
    assert [e["v"] for e in snap["edges"]] == [1, 0]
    assert snap["edges"][0]["t"] == pytest.approx(0.100, abs=0.002)
    assert snap["edges"][1]["t"] == pytest.approx(0.200, abs=0.002)
    assert snap["counters"]["edges"] == 2


def test_initial_level_is_not_an_edge():
    sampler, _ = make_sampler(lambda now: True)  # line already HIGH at start
    run_ticks(sampler, 0.0, 0.1)
    snap = sampler.snapshot_since(0)
    assert snap["edges"] == []
    assert snap["level"] == 1


def test_ring_buffer_evicts_old_edges():
    # A 50 ms pulse every 0.5 s, retention of 1 s.
    fn = lambda now: (now % 0.5) < 0.05
    sampler, _ = make_sampler(fn, retention_s=1.0)
    run_ticks(sampler, 0.0, 3.0)
    snap = sampler.snapshot_since(0)
    assert snap["edges"]  # still streaming
    assert all(e["t"] >= 3.0 - 1.0 - 0.002 for e in snap["edges"])
    assert snap["counters"]["edges"] > len(snap["edges"])  # old ones evicted, not uncounted


def test_edges_since_seq():
    sampler, _ = make_sampler(pulse(0.1, 0.2))
    run_ticks(sampler, 0.0, 0.3)
    _, latest = sse_payload(sampler, 0)
    assert latest == 2
    payload, again = sse_payload(sampler, latest)
    assert payload["edges"] == []
    assert again == latest


# ----------------------------------------------------- pulse classification


def test_short_pulse_counts_as_glitch():
    sampler, _ = make_sampler(pulse(0.100, 0.105))  # 5 ms < 20 ms
    run_ticks(sampler, 0.0, 0.2)
    counters = sampler.status()["counters"]
    assert counters["glitches"] == 1
    assert counters["presses"] == 0


def test_long_pulse_counts_as_press():
    sampler, _ = make_sampler(pulse(0.1, 0.2))  # 100 ms >= 20 ms
    run_ticks(sampler, 0.0, 0.3)
    counters = sampler.status()["counters"]
    assert counters["presses"] == 1
    assert counters["glitches"] == 0


def test_pull_up_classifies_low_pulses():
    # Idle HIGH; a "press" pulls the line LOW for 100 ms.
    fn = lambda now: not (0.1 <= now < 0.2)
    sampler, _ = make_sampler(fn, pull="up")
    run_ticks(sampler, 0.0, 0.3)
    counters = sampler.status()["counters"]
    assert counters["presses"] == 1
    assert counters["glitches"] == 0


def test_reset_counters_keeps_window():
    sampler, _ = make_sampler(pulse(0.1, 0.2))
    run_ticks(sampler, 0.0, 0.3)
    sampler.reset_counters()
    snap = sampler.snapshot_since(0)
    assert snap["counters"]["edges"] == 0
    assert len(snap["edges"]) == 2  # graph window untouched


# ----------------------------------------------------------------- mock mode


def test_mock_signal_waveform():
    ms = MockSignal(22, "down")
    assert ms.read(0.1) is False          # idle
    assert ms.read(0.8) is True           # mid-press
    assert ms.read(5.002) is True         # the 4 ms glitch
    assert ms.read(5.2) is False
    # Pull-up: same activity, inverted electrical level.
    up = MockSignal(22, "up")
    assert up.read(0.1) is True
    assert up.read(0.8) is False


def test_mock_signal_through_sampler():
    sampler = Sampler(MockSignal, 22, "down")
    run_ticks(sampler, 0.0, 6.0)  # one full cycle at 1 kHz
    counters = sampler.status()["counters"]
    assert counters["presses"] == 2          # the two long presses
    assert counters["glitches"] >= 3         # bounce blips + the 4 ms glitch


# -------------------------------------------------------------- live pin swap


def test_set_input_switches_pin_and_clears():
    sampler, sources = make_sampler(pulse(0.1, 0.2))
    run_ticks(sampler, 0.0, 0.3)
    epoch_before = sampler.status()["epoch"]

    ok, err = sampler.set_input(17, "up")
    assert ok and err == ""
    assert sources[0].closed
    status = sampler.status()
    assert (status["pin"], status["pull"]) == (17, "up")
    assert status["epoch"] == epoch_before + 1
    assert status["counters"]["edges"] == 0
    assert sampler.snapshot_since(0)["edges"] == []


def test_set_input_failure_keeps_old_pin():
    sources = []

    def factory(pin, pull):
        if pin == 17:
            raise RuntimeError("GPIO17 is busy")
        src = ScriptedSource(pulse(0.1, 0.2), pin, pull)
        sources.append(src)
        return src

    sampler = Sampler(factory, 22, "down")
    run_ticks(sampler, 0.0, 0.3)

    ok, err = sampler.set_input(17, "down")
    assert not ok and "busy" in err
    status = sampler.status()
    assert (status["pin"], status["pull"]) == (22, "down")
    assert status["active"]
    assert not sources[0].closed
    assert status["counters"]["edges"] == 2  # untouched


def test_pull_change_failure_reclaims_old_pull():
    calls = []

    def factory(pin, pull):
        calls.append((pin, pull))
        if pull == "up":
            raise RuntimeError("no can do")
        return ScriptedSource(lambda now: False, pin, pull)

    sampler = Sampler(factory, 22, "down")
    ok, err = sampler.set_input(22, "up")  # same pin → release-then-claim path
    assert not ok and "no can do" in err
    status = sampler.status()
    assert (status["pin"], status["pull"]) == (22, "down")
    assert status["active"]  # old line successfully re-claimed
    assert calls == [(22, "down"), (22, "up"), (22, "down")]


def test_pull_change_double_failure_enters_no_pin_state():
    state = {"fail": False}

    def factory(pin, pull):
        if state["fail"]:
            raise RuntimeError("gone")
        return ScriptedSource(lambda now: False, pin, pull)

    sampler = Sampler(factory, 22, "down")
    state["fail"] = True
    ok, err = sampler.set_input(22, "up")
    assert not ok
    status = sampler.status()
    assert not status["active"]
    assert "re-claim" in status["error"]
    sampler.tick(1.0)  # NO PIN state must not crash the polling loop


# ------------------------------------------------------------------ web app


@pytest.fixture
def client():
    sampler = Sampler(MockSignal, 22, "down")
    run_ticks(sampler, 0.0, 1.5)
    app = create_app(sampler, "mock", window_s=10.0)
    app.config["TESTING"] = True
    return app.test_client()


def test_index_page(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"ButtonScope" in resp.data
    assert b"scope" in resp.data  # the canvas


def test_status_shape(client):
    s = client.get("/status").get_json()
    for key in ("pin", "pull", "mode", "window_s", "sample_hz", "glitch_ms",
                "level", "idle", "active", "epoch", "counters"):
        assert key in s
    assert s["mode"] == "mock"
    assert s["pin"] == 22
    assert s["window_s"] == 10.0
    for key in ("edges", "glitches", "presses", "achieved_hz"):
        assert key in s["counters"]


def test_config_switches_pin(client):
    resp = client.post("/config", json={"pin": 5, "pull": "up"})
    assert resp.status_code == 200
    status = resp.get_json()["status"]
    assert (status["pin"], status["pull"]) == (5, "up")
    assert status["counters"]["edges"] == 0


def test_config_rejects_bad_input(client):
    assert client.post("/config", json={"pin": 99, "pull": "down"}).status_code == 400
    assert client.post("/config", json={"pin": "22", "pull": "down"}).status_code == 400
    assert client.post("/config", json={"pin": 5, "pull": "sideways"}).status_code == 400
    assert client.post("/config", json={}).status_code == 400
    # Nothing changed
    assert client.get("/status").get_json()["pin"] == 22


def test_config_claim_failure_returns_409():
    def factory(pin, pull):
        if pin != 22:
            raise RuntimeError(f"GPIO{pin} is busy")
        return MockSignal(pin, pull)

    sampler = Sampler(factory, 22, "down")
    app = create_app(sampler, "mock")
    resp = app.test_client().post("/config", json={"pin": 17, "pull": "down"})
    assert resp.status_code == 409
    body = resp.get_json()
    assert "busy" in body["error"]
    assert body["status"]["pin"] == 22
    assert body["status"]["active"]


def test_reset_endpoint(client):
    assert client.get("/status").get_json()["counters"]["edges"] > 0
    resp = client.post("/reset")
    assert resp.status_code == 200
    assert resp.get_json()["status"]["counters"]["edges"] == 0


def test_sse_payload_shape():
    sampler = Sampler(MockSignal, 22, "down")
    run_ticks(sampler, 0.0, 1.5)
    payload, latest = sse_payload(sampler, 0)
    for key in ("now", "epoch", "level", "idle", "active", "pin", "pull",
                "edges", "counters"):
        assert key in payload
    assert latest > 0
    assert payload["edges"]
    assert set(payload["edges"][0]) == {"t", "v"}
