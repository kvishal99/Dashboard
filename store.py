"""SQLite persistence for partner counts and website health checks.

This is the dashboard's own database - the partner MySQL is never written to.
A fresh connection is opened per operation (cheap for SQLite) so the store is
safe to use from the scheduler's threads and from request handlers.
"""
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS partner_counts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    partner      TEXT NOT NULL,
    feed_total   INTEGER,   -- COUNT(id) WHERE partner = ?  (what WE hold)
    db_future    INTEGER,   -- published = 1 AND enddates >= today
    db_past      INTEGER,   -- published = 1 AND enddates <  today
    last_created TEXT,      -- MAX(created): when the job last inserted anything
    ok           INTEGER NOT NULL DEFAULT 1,
    error        TEXT,
    duration_ms  INTEGER,
    collected_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_counts_lookup
    ON partner_counts (partner, collected_at DESC);

-- What the PARTNER has on their side, which is not in our MySQL at all.
-- Reported independently of the DB scan: by an ingest script at the end of a
-- run, by a feed-file counter, or pushed by hand. Kept separate from
-- partner_counts because it arrives on its own cadence.
CREATE TABLE IF NOT EXISTS partner_feed_counts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    partner     TEXT NOT NULL,
    feed_count  INTEGER,      -- records the partner's feed/API actually had
    inserted    INTEGER,      -- how many that run inserted (optional)
    source      TEXT,         -- script | file | api | manual
    note        TEXT,
    reported_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_feed_lookup
    ON partner_feed_counts (partner, reported_at DESC);

-- Every crontab line on every reachable server. Refreshed wholesale per server
-- by collect-crons.py, so a job removed from a crontab disappears here too.
CREATE TABLE IF NOT EXISTS cron_jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    server        TEXT NOT NULL,      -- the IP we connected to
    hostname      TEXT,               -- what the box calls itself
    line_no       INTEGER,            -- position in the crontab
    schedule      TEXT,               -- raw 5-field spec, or @daily etc
    schedule_human TEXT,              -- best-effort plain English
    command       TEXT NOT NULL,
    name          TEXT,               -- short human label, e.g. marriott_mvc/mvc.sh
    script        TEXT,               -- the path being run
    partner       TEXT,               -- guessed from the path, may be null
    log_file      TEXT,               -- redirect target, if the line has one
    log_mtime     REAL,               -- when that file was last written
    log_size      INTEGER,
    disabled      INTEGER NOT NULL DEFAULT 0,   -- commented-out line
    collected_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cron_server ON cron_jobs (server, line_no);

CREATE TABLE IF NOT EXISTS site_checks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    site_name   TEXT NOT NULL,
    url         TEXT NOT NULL,
    ok          INTEGER NOT NULL,
    status_code INTEGER,
    latency_ms  INTEGER,
    error       TEXT,
    checked_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_check_lookup
    ON site_checks (url, checked_at DESC);

-- One row per site: is it currently considered up or down, since when, and
-- when we last mailed about it. Persisted rather than kept in memory so a
-- restart of the dashboard cannot re-send a DOWN mail for an outage that has
-- already been reported, and so "down since" survives a restart.
CREATE TABLE IF NOT EXISTS site_alert_state (
    url        TEXT PRIMARY KEY,
    state      TEXT NOT NULL,     -- up | down
    since      REAL NOT NULL,     -- when it entered that state
    last_sent  REAL               -- last mail sent about this site
);

-- Every alert mail we tried to send, delivered or not. A failed send is
-- recorded too: an outage nobody was told about is exactly what you want to
-- find out later.
CREATE TABLE IF NOT EXISTS alert_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    url         TEXT NOT NULL,
    site_name   TEXT,
    kind        TEXT NOT NULL,    -- down | reminder | recovery | test
    recipients  TEXT,
    subject     TEXT,
    ok          INTEGER NOT NULL,
    error       TEXT,
    sent_at     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alert_log_time ON alert_log (sent_at DESC);
"""


class Store:
    def __init__(self, path: str):
        self.path = path
        self.init_schema()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init_schema(self) -> None:
        with self._conn() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            # Migration for databases created before db_past existed. Older rows
            # keep NULL, which the UI renders as "-" rather than a wrong zero.
            existing = {r["name"] for r in conn.execute("PRAGMA table_info(partner_counts)")}
            if "db_past" not in existing:
                conn.execute("ALTER TABLE partner_counts ADD COLUMN db_past INTEGER")
            if "last_created" not in existing:
                conn.execute("ALTER TABLE partner_counts ADD COLUMN last_created TEXT")

    # -------------------------------------------------------- partner counts

    def record_counts(
        self,
        partner: str,
        feed_total: Optional[int],
        db_future: Optional[int],
        ok: bool,
        db_past: Optional[int] = None,
        last_created: Optional[str] = None,
        error: Optional[str] = None,
        duration_ms: Optional[int] = None,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO partner_counts
                   (partner, feed_total, db_future, db_past, last_created, ok,
                    error, duration_ms, collected_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (partner, feed_total, db_future, db_past, last_created,
                 1 if ok else 0, error, duration_ms, time.time()),
            )

    def latest_counts(self) -> Dict[str, Dict[str, Any]]:
        """Most recent row per partner, keyed by partner name."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT c.* FROM partner_counts c
                   JOIN (
                       SELECT partner, MAX(id) AS max_id
                       FROM partner_counts GROUP BY partner
                   ) m ON m.max_id = c.id"""
            ).fetchall()
        return {r["partner"]: dict(r) for r in rows}

    def previous_counts(self) -> Dict[str, Dict[str, Any]]:
        """Second-most-recent successful row per partner, for deltas."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT c.* FROM partner_counts c
                   JOIN (
                       SELECT partner, MAX(id) AS max_id
                       FROM partner_counts
                       WHERE ok = 1 AND id NOT IN (
                           SELECT MAX(id) FROM partner_counts WHERE ok = 1
                           GROUP BY partner
                       )
                       GROUP BY partner
                   ) m ON m.max_id = c.id"""
            ).fetchall()
        return {r["partner"]: dict(r) for r in rows}

    def last_counts_at(self) -> Optional[float]:
        """When any partner count was last collected, or None if never.

        The hourly scheduler reads this on startup to decide whether a restart
        deserves an immediate sweep or should just wait for the next hour.
        """
        with self._conn() as conn:
            row = conn.execute(
                "SELECT MAX(collected_at) AS last FROM partner_counts"
            ).fetchone()
        return row["last"] if row and row["last"] is not None else None

    def counts_history(self, partner: str, limit: int = 60) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM partner_counts WHERE partner = ?
                   ORDER BY id DESC LIMIT ?""",
                (partner, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------- partner-side feed

    def record_feed_count(
        self,
        partner: str,
        feed_count: Optional[int],
        inserted: Optional[int] = None,
        source: str = "manual",
        note: Optional[str] = None,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO partner_feed_counts
                   (partner, feed_count, inserted, source, note, reported_at)
                   VALUES (?,?,?,?,?,?)""",
                (partner, feed_count, inserted, source, note, time.time()),
            )

    def latest_feed_counts(self) -> Dict[str, Dict[str, Any]]:
        """Most recent partner-side report per partner."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT f.* FROM partner_feed_counts f
                   JOIN (
                       SELECT partner, MAX(id) AS max_id
                       FROM partner_feed_counts GROUP BY partner
                   ) m ON m.max_id = f.id"""
            ).fetchall()
        return {r["partner"]: dict(r) for r in rows}

    def feed_history(self, partner: str, limit: int = 60) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM partner_feed_counts WHERE partner = ?
                   ORDER BY id DESC LIMIT ?""",
                (partner, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    # --------------------------------------------------------------- cron jobs

    def replace_cron_jobs(self, server: str, hostname: Optional[str],
                          rows: List[Dict[str, Any]]) -> int:
        """Swap in a server's whole crontab.

        Deleting first is deliberate: a job removed on the server must vanish
        here rather than linger as a phantom entry forever.
        """
        now = time.time()
        with self._conn() as conn:
            conn.execute("DELETE FROM cron_jobs WHERE server = ?", (server,))
            conn.executemany(
                """INSERT INTO cron_jobs
                   (server, hostname, line_no, schedule, schedule_human, command,
                    name, script, partner, log_file, log_mtime, log_size, disabled,
                    collected_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [(server, hostname, r.get("line_no"), r.get("schedule"),
                  r.get("schedule_human"), r["command"], r.get("name"),
                  r.get("script"),
                  r.get("partner"), r.get("log_file"), r.get("log_mtime"),
                  r.get("log_size"), 1 if r.get("disabled") else 0, now)
                 for r in rows],
            )
        return len(rows)

    def cron_jobs(self) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM cron_jobs ORDER BY server, line_no"
            ).fetchall()
        return [dict(r) for r in rows]

    def cron_servers(self) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT server, hostname, COUNT(*) AS jobs,
                          SUM(disabled) AS disabled, MAX(collected_at) AS collected_at
                   FROM cron_jobs GROUP BY server ORDER BY jobs DESC"""
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------ site checks

    def record_check(
        self,
        site_name: str,
        url: str,
        ok: bool,
        status_code: Optional[int],
        latency_ms: Optional[int],
        error: Optional[str],
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO site_checks
                   (site_name, url, ok, status_code, latency_ms, error, checked_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (site_name, url, 1 if ok else 0, status_code, latency_ms,
                 error, time.time()),
            )

    def latest_checks(self) -> Dict[str, Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT c.* FROM site_checks c
                   JOIN (
                       SELECT url, MAX(id) AS max_id FROM site_checks GROUP BY url
                   ) m ON m.max_id = c.id"""
            ).fetchall()
        return {r["url"]: dict(r) for r in rows}

    def uptime_since(self, url: str, since_ts: float) -> Dict[str, Any]:
        with self._conn() as conn:
            row = conn.execute(
                """SELECT COUNT(*) AS checks, SUM(ok) AS up, AVG(latency_ms) AS avg_latency
                   FROM site_checks WHERE url = ? AND checked_at >= ?""",
                (url, since_ts),
            ).fetchone()
        checks, up = row["checks"] or 0, row["up"] or 0
        return {
            "checks": checks,
            "uptime_pct": round(100.0 * up / checks, 2) if checks else None,
            "avg_latency_ms": round(row["avg_latency"]) if row["avg_latency"] else None,
        }

    def check_history(self, url: str, limit: int = 60) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM site_checks WHERE url = ? ORDER BY id DESC LIMIT ?",
                (url, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    # ----------------------------------------------------------------- alerts

    def alert_state(self, url: str) -> Optional[Dict[str, Any]]:
        """Current up/down state for a site, or None if we've never alerted on it."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM site_alert_state WHERE url = ?", (url,)
            ).fetchone()
        return dict(row) if row else None

    def save_alert_state(self, url: str, state: str, since: float,
                         last_sent: Optional[float]) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO site_alert_state (url, state, since, last_sent)
                   VALUES (?,?,?,?)
                   ON CONFLICT(url) DO UPDATE SET
                       state = excluded.state,
                       since = excluded.since,
                       last_sent = excluded.last_sent""",
                (url, state, since, last_sent),
            )

    def record_alert(self, url: str, site_name: str, kind: str,
                     recipients: List[str], ok: bool,
                     error: Optional[str] = None,
                     subject: Optional[str] = None) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO alert_log
                   (url, site_name, kind, recipients, subject, ok, error, sent_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (url, site_name, kind, ", ".join(recipients), subject,
                 1 if ok else 0, error, time.time()),
            )

    def recent_alerts(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM alert_log ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def prune(self, keep_days: int = 90) -> None:
        cutoff = time.time() - keep_days * 86400
        with self._conn() as conn:
            conn.execute("DELETE FROM partner_counts WHERE collected_at < ?", (cutoff,))
            conn.execute("DELETE FROM site_checks WHERE checked_at < ?", (cutoff,))
            # site_alert_state is deliberately not pruned - it is one row per
            # site and losing it would re-send alerts for ongoing outages.
            conn.execute("DELETE FROM alert_log WHERE sent_at < ?", (cutoff,))
