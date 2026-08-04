"""CSV generation for the Downloads section.

Every table in the dashboard should be removable from it - into a mail, a
spreadsheet, a ticket - so each one has an export here rather than a "select the
rows and copy" instruction.

Two decisions worth stating:

**Columns are declared, not inferred.** Dumping `dict.keys()` would export
internal fields (`job_rank`, `_keys`) and would silently change shape whenever a
field is added upstream. Each export lists its columns and their headers, so the
file someone automates against stays stable.

**Rows are yielded, not accumulated.** An export of 425 cron lines does not need
streaming, but the missing-records export for a large partner can be tens of
thousands of rows, and the same code path serves both.
"""
import csv
import io
import re
from typing import Any, Dict, Iterable, Iterator, List, Sequence, Tuple

# (key in the row dict, column header in the file)
Column = Tuple[str, str]

PARTNER_COLUMNS: List[Column] = [
    ("name", "Partner"),
    ("status", "Status"),
    ("issue_count", "Issues"),
    ("partner_feed_count", "In partner feed"),
    ("feed_total", "Total in database"),
    ("db_future", "Live (today & future)"),
    ("db_past", "Already ended"),
    ("db_unpublished", "Not published"),
    ("live_pct", "% still live"),
    ("delta_future", "Change since last count"),
    ("job_state", "Ingest job"),
    ("job_days_since", "Days since last insert"),
    ("cron_status", "Cron entry"),
    ("server", "Server"),
    ("frequency", "Frequency"),
    ("last_created", "Last record created"),
    ("collected_at_iso", "Last counted"),
    ("error", "Error"),
]

ISSUE_COLUMNS: List[Column] = [
    ("severity", "Status"),
    ("subject", "Who"),
    ("title", "What happened"),
    ("detail", "Detail"),
    ("action", "What to do"),
    ("process", "Process"),
    ("last_run_iso", "Last run"),
    ("scope", "Area"),
    ("kind", "Kind"),
    ("since_iso", "Since"),
]

COMPARISON_COLUMNS: List[Column] = [
    ("title", "Title"),
    ("external_id", "ID"),
    ("start_date", "Start date"),
    ("end_date", "End date"),
    ("venue", "Venue"),
    ("url", "URL"),
]

CRON_COLUMNS: List[Column] = [
    ("server", "Server"),
    ("hostname", "Hostname"),
    ("name", "Job"),
    ("category_label", "Category"),
    ("schedule", "Schedule"),
    ("schedule_human", "Schedule (plain)"),
    ("partner", "Partner"),
    ("disabled", "Disabled"),
    ("log_file", "Log file"),
    ("log_age_days", "Log age (days)"),
    ("command", "Full command"),
]

SITE_COLUMNS: List[Column] = [
    ("name", "Site"),
    ("url", "URL"),
    ("ok", "Up"),
    ("status_code", "HTTP status"),
    ("latency_ms", "Latency (ms)"),
    ("uptime_24h", "Uptime 24h %"),
    ("checked_at_iso", "Last checked"),
    ("error", "Error"),
]

HISTORY_COLUMNS: List[Column] = [
    ("collected_at_iso", "Collected"),
    ("feed_total", "Total in database"),
    ("db_future", "Live"),
    ("db_past", "Ended"),
    ("db_unpublished", "Not published"),
    ("duration_ms", "Query time (ms)"),
    ("ok", "OK"),
    ("error", "Error"),
]

LOG_COLUMNS: List[Column] = [
    ("time", "Time"),
    ("level", "Level"),
    ("logger", "Logger"),
    ("status", "HTTP status"),
    ("message", "Message"),
]


def _cell(value: Any) -> Any:
    """Render one value for CSV.

    Booleans become yes/no rather than True/False because these files are read
    in Excel by people, not parsed by code. None becomes empty, never the string
    "None".
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    return value


def to_csv(rows: Iterable[Dict[str, Any]], columns: Sequence[Column]) -> Iterator[str]:
    """Stream a CSV, header first.

    utf-8-sig is written by the caller's media type; the BOM matters because
    Excel otherwise renders any non-ASCII partner or venue name as mojibake.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")

    def flush() -> str:
        buffer.seek(0)
        text = buffer.read()
        buffer.seek(0)
        buffer.truncate(0)
        return text

    writer.writerow([header for _, header in columns])
    yield flush()

    for row in rows:
        writer.writerow([_cell(row.get(key)) for key, _ in columns])
        yield flush()


def safe_filename(*parts: str) -> str:
    """A download filename that cannot escape a directory or break a header.

    Partner names reach this from the URL, so anything outside a small alphabet
    is replaced rather than trusted.
    """
    cleaned = [re.sub(r"[^A-Za-z0-9._-]+", "-", str(p)).strip("-") for p in parts if p]
    return "-".join(c for c in cleaned if c) or "export"
