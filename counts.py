"""Counting partner events.

`collect_all` is the one the scheduler uses: a single grouped query returns
every partner in one table scan. `collect` re-counts a single partner and backs
the per-partner "Refresh" button.

Both are blocking - the scheduler calls them in a thread pool.
"""
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import mysql


@dataclass
class CountResult:
    feed_total: Optional[int] = None
    db_future: Optional[int] = None
    db_past: Optional[int] = None
    # MAX(created) - when this partner's ingest job last inserted anything.
    # The cheapest possible "is the cron still working" signal: it comes free
    # with the grouped scan and needs no access to the servers themselves.
    last_created: Optional[str] = None
    ok: bool = True
    error: Optional[str] = None
    duration_ms: int = 0

    @property
    def db_unpublished(self) -> Optional[int]:
        """Inserted but never went live - derived, so it costs no extra query."""
        if None in (self.feed_total, self.db_future, self.db_past):
            return None
        return max(self.feed_total - self.db_future - self.db_past, 0)


def collect(
    partner: str, database: Dict[str, Any], queries: Dict[str, str]
) -> CountResult:
    """Count everything we hold for `partner` and break it down by state.

    Never raises: a failure comes back as ok=False with the reason attached, so
    one unreachable partner cannot stop the rest of the cycle.
    """
    started = time.perf_counter()
    try:
        feed_total = mysql.scalar(database, queries["feed_total"], (partner,))
        db_future = mysql.scalar(database, queries["db_future"], (partner,))
        db_past = mysql.scalar(database, queries["db_past"], (partner,))
        result = CountResult(
            feed_total=feed_total, db_future=db_future, db_past=db_past, ok=True
        )
    except Exception as exc:
        result = CountResult(ok=False, error=f"{type(exc).__name__}: {exc}")
    result.duration_ms = int((time.perf_counter() - started) * 1000)
    return result


@dataclass
class SweepResult:
    """Outcome of one grouped scan across every partner."""
    partners: Dict[str, CountResult]
    ok: bool = True
    error: Optional[str] = None
    duration_ms: int = 0


def collect_all(database: Dict[str, Any], queries: Dict[str, str]) -> SweepResult:
    """Count every partner in a single grouped query.

    One scan of jos_eventlist_events returns totals for all partners at once,
    which is far cheaper than issuing three counts per partner. Never raises.
    """
    started = time.perf_counter()
    try:
        rows = mysql.rows(database, queries["all_partners"])
        partners = {}
        for row in rows:
            name = row[0]
            if name is None or not str(name).strip():
                continue
            partners[str(name).strip()] = CountResult(
                feed_total=int(row[1] or 0),
                db_future=int(row[2] or 0),
                db_past=int(row[3] or 0),
                last_created=str(row[4]) if len(row) > 4 and row[4] else None,
                ok=True,
            )
        result = SweepResult(partners=partners, ok=True)
    except Exception as exc:
        result = SweepResult(
            partners={}, ok=False, error=f"{type(exc).__name__}: {exc}"
        )
    result.duration_ms = int((time.perf_counter() - started) * 1000)
    return result
