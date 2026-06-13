"""Analytics event store (SPECS §11.4): a SQLite-backed log of play events.

Two event types are recorded from the controller:
- ``play_started``  — a track actually started (idle-start or switch), physical or
  web press. Feeds the hours-of-day and per-day graphs.
- ``play_completed`` — a track reached its natural end. Feeds the favorites ranking.

Resilience contract (D14): every DB call is guarded so a failure is logged and
swallowed — analytics must NEVER raise into playback/relay logic. The controller is
given a :class:`NullAnalytics` by default so it (and existing tests) construct unchanged.

Timestamps use a wall-clock source (``datetime.now``), independent of the controller's
monotonic clock, because "hour of day" / "which day" are meaningless on monotonic time.
"""

from __future__ import annotations

import abc
import datetime as _dt
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Callable, List, Optional

log = logging.getLogger(__name__)

PLAY_STARTED = "play_started"
PLAY_COMPLETED = "play_completed"


class Analytics(abc.ABC):
    @abc.abstractmethod
    def record_play_started(self, track: Optional[str]) -> None: ...

    @abc.abstractmethod
    def record_play_completed(self, track: Optional[str]) -> None: ...

    @abc.abstractmethod
    def revision(self) -> int:
        """Monotonic count of recorded events; lets the dashboard refetch only on change."""

    @abc.abstractmethod
    def aggregates(self, top_n: int) -> dict: ...

    @property
    def enabled(self) -> bool:
        return True

    def close(self) -> None:  # pragma: no cover - trivial
        pass


class NullAnalytics(Analytics):
    """No-op store: used when analytics is disabled or in code paths that don't need it."""

    def record_play_started(self, track: Optional[str]) -> None:
        pass

    def record_play_completed(self, track: Optional[str]) -> None:
        pass

    def revision(self) -> int:
        return 0

    def aggregates(self, top_n: int) -> dict:
        return {
            "by_hour": [{"hour": h, "count": 0} for h in range(24)],
            "top_tracks": [],
            "by_day": [],
            "peak_day": None,
            "total_plays": 0,
            "total_completions": 0,
        }

    @property
    def enabled(self) -> bool:
        return False


class SqliteAnalytics(Analytics):
    """Thread-safe SQLite event store (single connection + lock; tiny event volume)."""

    def __init__(self, db_path: Path, now_fn: Callable[[], _dt.datetime] = _dt.datetime.now):
        self._now = now_fn
        self._lock = threading.Lock()
        self._rev = 0
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: writes come from supervisor/button threads, reads from web
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS events (
                   id    INTEGER PRIMARY KEY AUTOINCREMENT,
                   ts    TEXT NOT NULL,
                   type  TEXT NOT NULL,
                   track TEXT
               )"""
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_type_ts ON events(type, ts)"
        )
        self._conn.commit()
        # Seed the revision from existing rows so a restart doesn't look "unchanged".
        cur = self._conn.execute("SELECT COUNT(*) FROM events")
        self._rev = int(cur.fetchone()[0])

    # -- recording -----------------------------------------------------------

    def _record(self, event_type: str, track: Optional[str]) -> None:
        ts = self._now().isoformat(timespec="seconds")
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO events (ts, type, track) VALUES (?, ?, ?)",
                    (ts, event_type, track),
                )
                self._conn.commit()
                self._rev += 1
        except Exception:
            # D14: analytics failures never propagate to playback/relays.
            log.exception("Analytics: failed to record %s for %r", event_type, track)

    def record_play_started(self, track: Optional[str]) -> None:
        self._record(PLAY_STARTED, track)

    def record_play_completed(self, track: Optional[str]) -> None:
        self._record(PLAY_COMPLETED, track)

    def revision(self) -> int:
        return self._rev

    # -- aggregation ---------------------------------------------------------

    def aggregates(self, top_n: int) -> dict:
        try:
            with self._lock:
                by_hour = self._by_hour()
                top_tracks = self._top_tracks(top_n)
                by_day = self._by_day()
                totals = self._totals()
        except Exception:
            log.exception("Analytics: aggregation query failed")
            return NullAnalytics().aggregates(top_n)

        peak_day = max(by_day, key=lambda d: d["count"], default=None)
        return {
            "by_hour": by_hour,
            "top_tracks": top_tracks,
            "by_day": by_day,
            "peak_day": peak_day,
            "total_plays": totals[0],
            "total_completions": totals[1],
        }

    def _by_hour(self) -> List[dict]:
        rows = self._conn.execute(
            "SELECT CAST(strftime('%H', ts) AS INTEGER) AS h, COUNT(*) "
            "FROM events WHERE type = ? GROUP BY h",
            (PLAY_STARTED,),
        ).fetchall()
        counts = {int(h): int(c) for h, c in rows if h is not None}
        return [{"hour": h, "count": counts.get(h, 0)} for h in range(24)]

    def _top_tracks(self, top_n: int) -> List[dict]:
        rows = self._conn.execute(
            "SELECT track, COUNT(*) AS c FROM events "
            "WHERE type = ? AND track IS NOT NULL "
            "GROUP BY track ORDER BY c DESC, track ASC LIMIT ?",
            (PLAY_COMPLETED, max(0, top_n)),
        ).fetchall()
        return [{"name": name, "count": int(c)} for name, c in rows]

    def _by_day(self) -> List[dict]:
        rows = self._conn.execute(
            "SELECT strftime('%Y-%m-%d', ts) AS d, COUNT(*) "
            "FROM events WHERE type = ? GROUP BY d ORDER BY d ASC",
            (PLAY_STARTED,),
        ).fetchall()
        return [{"day": d, "count": int(c)} for d, c in rows if d is not None]

    def _totals(self) -> tuple:
        plays = self._conn.execute(
            "SELECT COUNT(*) FROM events WHERE type = ?", (PLAY_STARTED,)
        ).fetchone()[0]
        completions = self._conn.execute(
            "SELECT COUNT(*) FROM events WHERE type = ?", (PLAY_COMPLETED,)
        ).fetchone()[0]
        return int(plays), int(completions)

    def close(self) -> None:
        try:
            with self._lock:
                self._conn.close()
        except Exception:
            log.exception("Analytics: error closing the database")


def create_analytics(cfg, db_path: Path) -> Analytics:
    """Real store when enabled; a no-op store otherwise. A creation failure (e.g. the
    DB path is unwritable) degrades to NullAnalytics so the app still runs."""
    if not cfg.analytics_enabled:
        log.info("Analytics disabled by config.")
        return NullAnalytics()
    try:
        store = SqliteAnalytics(db_path)
        log.info("Analytics store at %s.", db_path)
        return store
    except Exception:
        log.exception("Analytics: could not open %s — analytics disabled this run.", db_path)
        return NullAnalytics()
