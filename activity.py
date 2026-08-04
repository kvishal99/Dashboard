"""One partner's process log - what happened to this partner, newest first.

The dashboard used to have a single global Logs page: every line the process
wrote, for every partner at once, which meant that answering "what happened to
WCities last night?" involved reading past a hundred lines about someone else.
This assembles the same answer per partner instead.

Nothing here is a new data source. Every entry is something the dashboard
already recorded and simply never showed in one place:

    counts        partner_counts        - the hourly sweep, and what it found
    feed reports  partner_feed_counts   - what the ingest script said it saw
    uploads       sheet_uploads         - a spreadsheet arriving
    comparisons   comparisons           - a diff being run, and its result
    exports       event_exports         - a CSV being generated
    cron output   cron_jobs.log_mtime   - when the partner's own job last wrote
    app log       dashboard.log         - lines that name this partner

Entries carry a level (ok / warn / bad / info) rather than a log level, because
the question being asked is "did this work?", not "which logger emitted it".
"""
import os
import time
from typing import Any, Dict, List, Optional

import applog


def _entry(ts: Optional[float], level: str, source: str, message: str,
           detail: str = "") -> Dict[str, Any]:
    return {
        "ts": ts,
        "level": level,           # ok | warn | bad | info
        "source": source,         # counts | feed | upload | comparison | export | cron | app
        "message": message,
        "detail": detail,
    }


def _n(value: Optional[int]) -> str:
    return "—" if value is None else f"{value:,}"


def from_counts(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Each hourly collection, and what it found."""
    entries = []
    for row in rows:
        if not row.get("ok"):
            entries.append(_entry(
                row.get("collected_at"), "bad", "counts",
                "Count query failed",
                row.get("error") or "No reason was recorded.",
            ))
            continue
        total = row.get("feed_total")
        live = row.get("db_future")
        entries.append(_entry(
            row.get("collected_at"), "ok", "counts",
            f"Counted {_n(total)} records · {_n(live)} live",
            f"ended {_n(row.get('db_past'))}"
            + (f" · query took {row['duration_ms'] / 1000:.1f}s"
               if row.get("duration_ms") else ""),
        ))
    return entries


def from_feed_reports(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """What the partner's own ingest run reported about its feed."""
    entries = []
    for row in rows:
        inserted = row.get("inserted")
        entries.append(_entry(
            row.get("reported_at"), "info", "feed",
            f"Partner feed reported {_n(row.get('feed_count'))} records"
            + (f" · {_n(inserted)} inserted" if inserted is not None else ""),
            f"source: {row.get('source') or 'unknown'}"
            + (f" · {row['note']}" if row.get("note") else ""),
        ))
    return entries


def from_uploads(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        _entry(
            row.get("uploaded_at"), "info", "upload",
            f"Spreadsheet uploaded: {row.get('filename')}",
            f"{_n(row.get('row_count'))} rows",
        )
        for row in rows
    ]


def from_comparisons(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    entries = []
    for row in rows:
        if not row.get("ok"):
            entries.append(_entry(
                row.get("computed_at"), "bad", "comparison",
                "Spreadsheet comparison failed",
                row.get("error") or "No reason was recorded.",
            ))
            continue
        missing = row.get("missing") or 0
        entries.append(_entry(
            row.get("computed_at"), "warn" if missing else "ok", "comparison",
            f"Compared against spreadsheet · {_n(row.get('matching'))} matching, "
            f"{_n(missing)} missing, {_n(row.get('extra'))} extra",
            f"matched on {row.get('strategy_label') or 'unknown key'}",
        ))
    return entries


def from_exports(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    entries = []
    for row in rows:
        status = row.get("status")
        if status == "done":
            entries.append(_entry(
                row.get("finished_at"), "ok", "export",
                f"CSV generated · {_n(row.get('rows_written'))} events",
                row.get("filename") or "",
            ))
        elif status == "failed":
            entries.append(_entry(
                row.get("finished_at") or row.get("requested_at"), "bad", "export",
                "CSV generation failed",
                row.get("error") or "No reason was recorded.",
            ))
        else:
            entries.append(_entry(
                row.get("started_at") or row.get("requested_at"), "info", "export",
                f"CSV generation {status}",
                f"{_n(row.get('rows_written'))} rows so far",
            ))
    return entries


def from_cron(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """When this partner's own crontab jobs last wrote to their log file.

    This is the closest thing to the partner's script reporting in. It is
    inferred from the redirect target's mtime, so it says the job produced
    output, not that it succeeded - the detail line says so rather than
    letting the entry imply more than it knows.
    """
    entries = []
    now = time.time()
    for row in rows:
        mtime = row.get("log_mtime")
        if not mtime:
            continue
        days = (now - mtime) / 86400.0
        empty = row.get("log_size") == 0
        entries.append(_entry(
            mtime,
            "warn" if (days > 30 or empty) else "ok",
            "cron",
            f"Cron job wrote output: {row.get('name')}",
            (f"{os.path.basename(row.get('log_file') or '')} is empty"
             if empty else
             f"{os.path.basename(row.get('log_file') or '')}, "
             f"{row.get('log_size') or 0:,} bytes")
            + " · output only, not a success signal",
        ))
    return entries


def from_app_log(path: str, partner: str, limit: int = 40) -> List[Dict[str, Any]]:
    """Lines in the dashboard's own log that name this partner.

    Matched on the partner name appearing in the line, which is what makes an
    entry partner-specific. A short partner name can of course appear inside an
    unrelated word; that is why these are labelled `app` and shown alongside the
    structured entries rather than presented as the partner's own log.
    """
    if not path or not os.path.isfile(path):
        return []
    try:
        result = applog.read(path, query=partner, limit=limit)
    except OSError:
        return []

    entries = []
    for line in result.get("lines", []):
        # HTTP access lines are dropped unless they represent a real failure
        # while handling this partner.
        #
        # The partner's name appears in the dashboard's OWN request URLs
        # (`GET /api/partners/venuepilot/logs 200`), so without this the feed
        # fills with a record of the page that is displaying it - 48 entries
        # for venuepilot, every one of them this page polling itself.
        #
        # 401 and 403 go too: those are the login wall, which says nothing
        # about the partner and would otherwise bury the real entries every
        # time someone hit the dashboard without credentials.
        status = line.get("status")
        if status is not None and (status < 400 or status in (401, 403)):
            continue

        level = line.get("level", "INFO")
        entries.append(_entry(
            line.get("ts"),
            "bad" if level in ("ERROR", "CRITICAL")
            else "warn" if level == "WARNING" else "info",
            "app",
            line.get("message") or line.get("raw") or "",
            f"{level} · {line.get('logger') or 'dashboard'}",
        ))
    return entries


def build(
    partner: str,
    counts: List[Dict[str, Any]],
    feed_reports: List[Dict[str, Any]],
    uploads: List[Dict[str, Any]],
    comparisons: List[Dict[str, Any]],
    exports: List[Dict[str, Any]],
    cron_rows: List[Dict[str, Any]],
    log_path: str = "",
    limit: int = 120,
) -> List[Dict[str, Any]]:
    """The partner's whole activity feed, newest first."""
    entries: List[Dict[str, Any]] = []
    entries += from_counts(counts)
    entries += from_feed_reports(feed_reports)
    entries += from_uploads(uploads)
    entries += from_comparisons(comparisons)
    entries += from_exports(exports)
    entries += from_cron(cron_rows)
    entries += from_app_log(log_path, partner)

    # Undated entries sort last rather than to the top: an unknown time is not
    # "just now", and putting it first would push real recent activity down.
    entries.sort(key=lambda e: (e["ts"] is not None, e["ts"] or 0), reverse=True)
    return entries[:limit]


def summarise(entries: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "total": len(entries),
        "errors": sum(1 for e in entries if e["level"] == "bad"),
        "warnings": sum(1 for e in entries if e["level"] == "warn"),
        "last_at": next((e["ts"] for e in entries if e["ts"]), None),
    }
