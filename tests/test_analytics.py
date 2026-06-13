"""Analytics event store: recording, aggregation, resilience, controller hooks."""

import datetime as dt

import pytest

from saintantoine.analytics import (
    NullAnalytics,
    SqliteAnalytics,
    create_analytics,
)
from saintantoine.config import Config
from saintantoine.controller import IDLE, PLAYING


class FakeWallClock:
    """Controllable wall-clock for deterministic hour/day bucketing."""

    def __init__(self, start: dt.datetime):
        self.t = start

    def now(self) -> dt.datetime:
        return self.t

    def set(self, *args):
        self.t = dt.datetime(*args)


@pytest.fixture
def store(tmp_path):
    clock = FakeWallClock(dt.datetime(2026, 6, 12, 9, 0, 0))
    a = SqliteAnalytics(tmp_path / "a.db", now_fn=clock.now)
    yield a, clock
    a.close()


def test_records_and_counts_totals(store):
    a, _ = store
    a.record_play_started("x.mp3")
    a.record_play_started("y.mp3")
    a.record_play_completed("x.mp3")
    agg = a.aggregates(top_n=10)
    assert agg["total_plays"] == 2
    assert agg["total_completions"] == 1


def test_revision_increments_per_event(store):
    a, _ = store
    assert a.revision() == 0
    a.record_play_started("x.mp3")
    a.record_play_completed("x.mp3")
    assert a.revision() == 2


def test_by_hour_buckets_play_starts(store):
    a, clock = store
    clock.set(2026, 6, 12, 9, 30)
    a.record_play_started("x.mp3")
    a.record_play_started("y.mp3")
    clock.set(2026, 6, 12, 14, 5)
    a.record_play_started("z.mp3")
    by_hour = {h["hour"]: h["count"] for h in a.aggregates(10)["by_hour"]}
    assert len(by_hour) == 24
    assert by_hour[9] == 2
    assert by_hour[14] == 1
    assert by_hour[0] == 0


def test_top_tracks_ranks_completions_only(store):
    a, _ = store
    for _ in range(3):
        a.record_play_completed("fav.mp3")
    a.record_play_completed("other.mp3")
    a.record_play_started("never_finished.mp3")  # start only -> not a favorite
    top = a.aggregates(top_n=10)["top_tracks"]
    assert top[0] == {"name": "fav.mp3", "count": 3}
    assert {"name": "other.mp3", "count": 1} in top
    assert all(t["name"] != "never_finished.mp3" for t in top)


def test_top_tracks_respects_limit(store):
    a, _ = store
    for name in ["a", "b", "c"]:
        a.record_play_completed(name)
    assert len(a.aggregates(top_n=2)["top_tracks"]) == 2


def test_by_day_and_peak_day(store):
    a, clock = store
    clock.set(2026, 6, 12, 10, 0)
    for _ in range(3):
        a.record_play_started("x.mp3")
    clock.set(2026, 6, 13, 10, 0)
    a.record_play_started("x.mp3")
    agg = a.aggregates(10)
    by_day = {d["day"]: d["count"] for d in agg["by_day"]}
    assert by_day == {"2026-06-12": 3, "2026-06-13": 1}
    assert agg["peak_day"] == {"day": "2026-06-12", "count": 3}


def test_revision_seeded_from_existing_rows(tmp_path):
    path = tmp_path / "a.db"
    a = SqliteAnalytics(path)
    a.record_play_started("x.mp3")
    a.close()
    b = SqliteAnalytics(path)  # reopen: revision must reflect persisted rows
    assert b.revision() == 1
    b.close()


def test_recording_failure_is_swallowed(store):
    a, _ = store
    a.close()  # subsequent writes raise inside, must NOT propagate
    a.record_play_started("x.mp3")  # no exception
    a.record_play_completed("x.mp3")


def test_null_analytics_is_noop():
    n = NullAnalytics()
    n.record_play_started("x")
    n.record_play_completed("x")
    assert n.revision() == 0
    assert n.enabled is False
    agg = n.aggregates(10)
    assert agg["total_plays"] == 0
    assert len(agg["by_hour"]) == 24
    assert agg["top_tracks"] == []
    assert agg["peak_day"] is None


def test_create_analytics_disabled_returns_null(tmp_path):
    cfg = Config()
    cfg.analytics_enabled = False
    a = create_analytics(cfg, tmp_path / "a.db")
    assert isinstance(a, NullAnalytics)


def test_create_analytics_enabled_returns_store(tmp_path):
    cfg = Config()
    a = create_analytics(cfg, tmp_path / "a.db")
    assert isinstance(a, SqliteAnalytics)
    a.close()


# -- controller integration -------------------------------------------------


class SpyAnalytics(NullAnalytics):
    def __init__(self):
        self.started = []
        self.completed = []
        self._rev = 0

    def record_play_started(self, track):
        self.started.append(track)
        self._rev += 1

    def record_play_completed(self, track):
        self.completed.append(track)
        self._rev += 1

    def revision(self):
        return self._rev


def test_press_records_play_started(make_harness):
    spy = SpyAnalytics()
    h = make_harness(analytics=spy)
    h.press()
    assert len(spy.started) == 1
    assert spy.started[0].endswith(".mp3")
    assert spy.completed == []


def test_switch_records_start_not_completion(make_harness):
    spy = SpyAnalytics()
    h = make_harness(analytics=spy)
    h.press()
    h.clock.advance(1.0)
    h.press()  # switch mid-play: a new start, but the first did NOT finish
    assert len(spy.started) == 2
    assert spy.completed == []


def test_natural_end_records_completion(make_harness):
    spy = SpyAnalytics()
    h = make_harness(analytics=spy)
    h.press()
    h.run_until(11.0)  # mock track duration is 10 s
    assert h.controller.state == IDLE
    assert len(spy.completed) == 1
    assert spy.completed[0].endswith(".mp3")


def test_status_exposes_analytics_rev(make_harness):
    spy = SpyAnalytics()
    h = make_harness(analytics=spy)
    assert h.controller.status()["analytics_rev"] == 0
    h.press()
    assert h.controller.status()["analytics_rev"] == 1
