"""SQLite persistence for partner counts and website health checks.

This is the dashboard's own database - the partner MySQL is never written to.
A fresh connection is opened per operation (cheap for SQLite) so the store is
safe to use from the scheduler's threads and from request handlers.
"""
import json
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
    -- What KIND of job this is: health | scraper | import | csv | maintenance |
    -- other. Every line used to be presented as a data insertion, which is what
    -- most of these are not - see cron_parse.CATEGORIES.
    category      TEXT,
    log_file      TEXT,               -- redirect target, if the line has one
    log_mtime     REAL,               -- when that file was last written
    log_size      INTEGER,
    disabled      INTEGER NOT NULL DEFAULT 0,   -- commented-out line
    collected_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cron_server ON cron_jobs (server, line_no);

-- An uploaded partner spreadsheet. The file itself is kept on disk (see
-- `stored_path`) so it can be downloaded back later - the source of a
-- comparison has to be retrievable, or a disputed result cannot be settled.
CREATE TABLE IF NOT EXISTS sheet_uploads (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    partner      TEXT NOT NULL,
    filename     TEXT NOT NULL,      -- what the user called it
    stored_path  TEXT,               -- where we put it
    size_bytes   INTEGER,
    row_count    INTEGER,
    headers      TEXT,               -- JSON: the sheet's own column names
    mapping      TEXT,               -- JSON: canonical field -> column used
    uploaded_at  REAL NOT NULL,
    note         TEXT
);
CREATE INDEX IF NOT EXISTS idx_upload_partner
    ON sheet_uploads (partner, uploaded_at DESC);

-- The normalised rows of one upload. Kept rather than recomputed so a
-- comparison can be re-run against fresh database counts without asking the
-- user to upload the same file again.
CREATE TABLE IF NOT EXISTS sheet_rows (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    upload_id   INTEGER NOT NULL,
    external_id TEXT,
    title       TEXT,
    start_date  TEXT,
    end_date    TEXT,
    venue       TEXT,
    url         TEXT
);
CREATE INDEX IF NOT EXISTS idx_sheet_rows_upload ON sheet_rows (upload_id);

-- The outcome of diffing one upload against the database.
CREATE TABLE IF NOT EXISTS comparisons (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    partner      TEXT NOT NULL,
    upload_id    INTEGER,
    sheet_total  INTEGER,
    db_total     INTEGER,
    matching     INTEGER,
    missing      INTEGER,
    extra        INTEGER,
    strategy     TEXT,               -- which key tied the two sides together
    strategy_label TEXT,
    match_rate   REAL,
    reliable     INTEGER,            -- 0 when the match rate is too low to trust
    sheet_duplicates INTEGER,
    db_duplicates    INTEGER,
    ok           INTEGER NOT NULL DEFAULT 1,
    error        TEXT,
    duration_ms  INTEGER,
    computed_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_comparison_partner
    ON comparisons (partner, computed_at DESC);

-- The rows behind the missing/extra counts. A number tells you there is a
-- problem; these are what someone actually works from, and what the CSV
-- download serves.
CREATE TABLE IF NOT EXISTS comparison_rows (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    comparison_id INTEGER NOT NULL,
    side          TEXT NOT NULL,     -- missing (in sheet only) | extra (in db only)
    title         TEXT,
    external_id   TEXT,
    start_date    TEXT,
    end_date      TEXT,
    venue         TEXT,
    url           TEXT
);
CREATE INDEX IF NOT EXISTS idx_comparison_rows
    ON comparison_rows (comparison_id, side);

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

-- A request to build a partner's event CSV from the database.
--
-- Kept as a job rather than generated inside the request because a large
-- partner is hundreds of thousands of rows: fever alone would hold a worker
-- for minutes and time the browser out long before the file existed. The row
-- carries its own progress, so the page can show "42,000 of 862,976" instead
-- of a spinner that says nothing.
CREATE TABLE IF NOT EXISTS event_exports (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    partner      TEXT NOT NULL,
    scope        TEXT NOT NULL DEFAULT 'all',  -- all | live | unpublished
    status       TEXT NOT NULL,                -- queued | running | done | failed
    rows_written INTEGER NOT NULL DEFAULT 0,
    total_rows   INTEGER,                      -- expected, from the count we hold
    filename     TEXT,
    stored_path  TEXT,
    size_bytes   INTEGER,
    error        TEXT,
    requested_at REAL NOT NULL,
    started_at   REAL,
    finished_at  REAL
);
CREATE INDEX IF NOT EXISTS idx_export_partner
    ON event_exports (partner, id DESC);
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

            # Cron rows collected before jobs were categorised. Left NULL rather
            # than back-filled with a guess: the next push from each server
            # re-parses every line and fills it in properly.
            cron_cols = {r["name"] for r in conn.execute("PRAGMA table_info(cron_jobs)")}
            if cron_cols and "category" not in cron_cols:
                conn.execute("ALTER TABLE cron_jobs ADD COLUMN category TEXT")

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
                    name, script, partner, category, log_file, log_mtime,
                    log_size, disabled, collected_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [(server, hostname, r.get("line_no"), r.get("schedule"),
                  r.get("schedule_human"), r["command"], r.get("name"),
                  r.get("script"),
                  r.get("partner"), r.get("category"), r.get("log_file"),
                  r.get("log_mtime"),
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

    def cron_jobs_for_partner(self, partner: str) -> List[Dict[str, Any]]:
        """Only this partner's crontab lines - matched case-insensitively.

        The partner-centric pages exist so that opening WCities shows WCities'
        jobs and nothing else, so the filter belongs in the query rather than in
        every caller.
        """
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM cron_jobs WHERE LOWER(partner) = LOWER(?)
                   ORDER BY server, line_no""",
                (partner,),
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

    # ------------------------------------------------- spreadsheet comparison

    # How many missing/extra rows are kept per comparison. The counts are always
    # exact; only the row listing is capped, and `rows_truncated` records when
    # it was - a silently short CSV would be worse than a stated limit.
    MAX_STORED_ROWS = 100_000

    def record_upload(
        self,
        partner: str,
        filename: str,
        stored_path: Optional[str],
        size_bytes: int,
        rows: List[Dict[str, Any]],
        headers: List[str],
        mapping: Dict[str, Any],
        note: Optional[str] = None,
    ) -> int:
        """Store an uploaded sheet and its normalised rows. Returns the upload id."""
        with self._conn() as conn:
            cursor = conn.execute(
                """INSERT INTO sheet_uploads
                   (partner, filename, stored_path, size_bytes, row_count,
                    headers, mapping, uploaded_at, note)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (partner, filename, stored_path, size_bytes, len(rows),
                 json.dumps(headers), json.dumps(mapping), time.time(), note),
            )
            upload_id = cursor.lastrowid
            conn.executemany(
                """INSERT INTO sheet_rows
                   (upload_id, external_id, title, start_date, end_date, venue, url)
                   VALUES (?,?,?,?,?,?,?)""",
                [(upload_id, r.get("external_id"), r.get("title"),
                  r.get("start_date"), r.get("end_date"), r.get("venue"), r.get("url"))
                 for r in rows],
            )
        return upload_id

    def uploads(self, partner: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM sheet_uploads"
        params: tuple = ()
        if partner:
            sql += " WHERE partner = ?"
            params = (partner,)
        sql += " ORDER BY id DESC LIMIT ?"
        with self._conn() as conn:
            rows = conn.execute(sql, params + (limit,)).fetchall()
        return [self._decode_upload(dict(r)) for r in rows]

    def upload(self, upload_id: int) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM sheet_uploads WHERE id = ?", (upload_id,)
            ).fetchone()
        return self._decode_upload(dict(row)) if row else None

    def latest_upload(self, partner: str) -> Optional[Dict[str, Any]]:
        found = self.uploads(partner, limit=1)
        return found[0] if found else None

    @staticmethod
    def _decode_upload(row: Dict[str, Any]) -> Dict[str, Any]:
        """JSON columns back into structures, tolerating rows written by hand."""
        for key in ("headers", "mapping"):
            try:
                row[key] = json.loads(row[key]) if row.get(key) else None
            except (TypeError, ValueError):
                row[key] = None
        return row

    def sheet_rows(self, upload_id: int) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM sheet_rows WHERE upload_id = ? ORDER BY id", (upload_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    def delete_upload(self, upload_id: int) -> None:
        """Remove a sheet and everything derived from it.

        Comparisons go too: a comparison whose source sheet no longer exists
        cannot be re-run or checked, so keeping it would leave a number on the
        page that nobody can account for.
        """
        with self._conn() as conn:
            comparison_ids = [
                r["id"] for r in conn.execute(
                    "SELECT id FROM comparisons WHERE upload_id = ?", (upload_id,)
                )
            ]
            for comparison_id in comparison_ids:
                conn.execute(
                    "DELETE FROM comparison_rows WHERE comparison_id = ?", (comparison_id,)
                )
            conn.execute("DELETE FROM comparisons WHERE upload_id = ?", (upload_id,))
            conn.execute("DELETE FROM sheet_rows WHERE upload_id = ?", (upload_id,))
            conn.execute("DELETE FROM sheet_uploads WHERE id = ?", (upload_id,))

    def record_comparison(
        self, partner: str, upload_id: Optional[int], result: Dict[str, Any]
    ) -> int:
        with self._conn() as conn:
            cursor = conn.execute(
                """INSERT INTO comparisons
                   (partner, upload_id, sheet_total, db_total, matching, missing,
                    extra, strategy, strategy_label, match_rate, reliable,
                    sheet_duplicates, db_duplicates, ok, error, duration_ms,
                    computed_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (partner, upload_id, result.get("sheet_total"), result.get("db_total"),
                 result.get("matching"), result.get("missing"), result.get("extra"),
                 result.get("strategy"), result.get("strategy_label"),
                 result.get("match_rate"),
                 1 if result.get("reliable") else 0,
                 result.get("sheet_duplicates"), result.get("db_duplicates"),
                 1 if result.get("ok") else 0, result.get("error"),
                 result.get("duration_ms"), time.time()),
            )
            comparison_id = cursor.lastrowid

            for side in ("missing", "extra"):
                rows = (result.get(f"{side}_rows") or [])[: self.MAX_STORED_ROWS]
                conn.executemany(
                    """INSERT INTO comparison_rows
                       (comparison_id, side, title, external_id, start_date,
                        end_date, venue, url)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    [(comparison_id, side, r.get("title"), r.get("external_id"),
                      r.get("start_date"), r.get("end_date"), r.get("venue"),
                      r.get("url")) for r in rows],
                )
        return comparison_id

    def latest_comparisons(self) -> Dict[str, Dict[str, Any]]:
        """Newest successful comparison per partner, for the cards and issues."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT c.* FROM comparisons c
                   JOIN (
                       SELECT partner, MAX(id) AS max_id
                       FROM comparisons WHERE ok = 1 GROUP BY partner
                   ) m ON m.max_id = c.id"""
            ).fetchall()
        return {r["partner"]: dict(r) for r in rows}

    def latest_comparison(self, partner: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM comparisons WHERE partner = ? ORDER BY id DESC LIMIT 1",
                (partner,),
            ).fetchone()
        return dict(row) if row else None

    def comparison(self, comparison_id: int) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM comparisons WHERE id = ?", (comparison_id,)
            ).fetchone()
        return dict(row) if row else None

    def comparison_history(self, partner: str, limit: int = 30) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM comparisons WHERE partner = ? ORDER BY id DESC LIMIT ?",
                (partner, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def comparison_rows(
        self, comparison_id: int, side: str, query: str = "",
        offset: int = 0, limit: int = 50,
    ) -> Dict[str, Any]:
        """One page of missing or extra rows, with the total for the pager."""
        where = "comparison_id = ? AND side = ?"
        params: List[Any] = [comparison_id, side]
        if query.strip():
            where += " AND (title LIKE ? OR external_id LIKE ? OR venue LIKE ? OR url LIKE ?)"
            params += [f"%{query.strip()}%"] * 4

        with self._conn() as conn:
            total = conn.execute(
                f"SELECT COUNT(*) AS n FROM comparison_rows WHERE {where}", params
            ).fetchone()["n"]
            rows = conn.execute(
                f"""SELECT * FROM comparison_rows WHERE {where}
                    ORDER BY start_date IS NULL, start_date, id
                    LIMIT ? OFFSET ?""",
                params + [limit, offset],
            ).fetchall()
        return {"rows": [dict(r) for r in rows], "total": total}

    def all_comparison_rows(self, comparison_id: int, side: str) -> List[Dict[str, Any]]:
        """Every stored row on one side - the CSV download, unpaginated."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM comparison_rows WHERE comparison_id = ? AND side = ?
                   ORDER BY start_date IS NULL, start_date, id""",
                (comparison_id, side),
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------------------------------------------------------- event exports

    def create_export(self, partner: str, scope: str = "all",
                      total_rows: Optional[int] = None) -> int:
        with self._conn() as conn:
            cursor = conn.execute(
                """INSERT INTO event_exports
                   (partner, scope, status, total_rows, requested_at)
                   VALUES (?,?,'queued',?,?)""",
                (partner, scope, total_rows, time.time()),
            )
        return cursor.lastrowid

    def update_export(self, export_id: int, **fields: Any) -> None:
        """Patch an export row. Only known columns, so a typo cannot write SQL."""
        allowed = {
            "status", "rows_written", "total_rows", "filename", "stored_path",
            "size_bytes", "error", "started_at", "finished_at",
        }
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return
        assignments = ", ".join(f"{k} = ?" for k in sets)
        with self._conn() as conn:
            conn.execute(
                f"UPDATE event_exports SET {assignments} WHERE id = ?",
                list(sets.values()) + [export_id],
            )

    def export(self, export_id: int) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM event_exports WHERE id = ?", (export_id,)
            ).fetchone()
        return dict(row) if row else None

    def exports(self, partner: Optional[str] = None, limit: int = 20) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM event_exports"
        params: tuple = ()
        if partner:
            sql += " WHERE partner = ?"
            params = (partner,)
        sql += " ORDER BY id DESC LIMIT ?"
        with self._conn() as conn:
            rows = conn.execute(sql, params + (limit,)).fetchall()
        return [dict(r) for r in rows]

    def latest_export(self, partner: str) -> Optional[Dict[str, Any]]:
        found = self.exports(partner, limit=1)
        return found[0] if found else None

    def active_export(self, partner: str) -> Optional[Dict[str, Any]]:
        """A queued or running export for this partner, if one exists.

        Used to make the Generate button idempotent: pressing it twice must
        join the run already in progress rather than start a second scan of the
        same partner.
        """
        with self._conn() as conn:
            row = conn.execute(
                """SELECT * FROM event_exports
                   WHERE partner = ? AND status IN ('queued','running')
                   ORDER BY id DESC LIMIT 1""",
                (partner,),
            ).fetchone()
        return dict(row) if row else None

    def delete_export(self, export_id: int) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM event_exports WHERE id = ?", (export_id,))

    def reset_running_exports(self) -> int:
        """Fail any export left mid-run by a restart.

        A row stuck at `running` with no process behind it would leave the page
        polling for a file that will never appear, and would block the next
        request through active_export().
        """
        with self._conn() as conn:
            cursor = conn.execute(
                """UPDATE event_exports
                   SET status = 'failed', finished_at = ?,
                       error = 'the dashboard restarted while this export was running'
                   WHERE status IN ('queued','running')""",
                (time.time(),),
            )
        return cursor.rowcount

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

            # Superseded comparisons and their rows. The newest per partner is
            # always kept regardless of age: it is what the partner card and the
            # issue list read, and an old comparison is still the only answer
            # available until someone uploads a fresher sheet.
            stale = [
                r["id"] for r in conn.execute(
                    """SELECT id FROM comparisons
                       WHERE computed_at < ? AND id NOT IN (
                           SELECT MAX(id) FROM comparisons GROUP BY partner
                       )""",
                    (cutoff,),
                )
            ]
            for comparison_id in stale:
                conn.execute(
                    "DELETE FROM comparison_rows WHERE comparison_id = ?", (comparison_id,)
                )
                conn.execute("DELETE FROM comparisons WHERE id = ?", (comparison_id,))
