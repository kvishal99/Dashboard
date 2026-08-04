"""Build a partner's real event data as a CSV, in the background.

The old per-partner download handed over count history - one row per hourly
collection - which is not the event data anyone actually wanted. This produces
the events themselves: one row per record we hold for that partner, with the
fields the team's spreadsheets carry.

Three things shape the implementation.

**It runs as a job, not inside the request.** `fever` holds ~862,000 records and
`wcities` ~42,000 live. Generating that inside a request would hold a worker for
minutes and time the browser out long before the file existed. The caller gets
an id back immediately and polls it; the row records `rows_written` against
`total_rows`, so the page shows "42,000 of 862,976" rather than a spinner.

**It pages the read.** Rows are pulled in `BATCH_ROWS` chunks with LIMIT/OFFSET
and written straight to disk, so peak memory is one batch regardless of whether
the partner has 200 events or 900,000.

**Venue names are resolved per batch, not joined.** `venue_details` holds one row
per partner submission - `wid` 92158 alone has 56 - so joining it into the event
query would multiply every event by the number of times its venue was ever
described. Instead each batch collects its `locid`s and resolves them with one
grouped lookup, which is both correct and one query per batch.

The column set is deliberately configurable (`exports.event_columns` in
config.yaml). The team's per-partner sheets live in Dropbox and are not readable
from here, so the default below covers the event fields this database actually
holds; adjust the list to match a specific sheet without touching code.
"""
import csv
import os
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import mysql

# How many event rows to read and write at a time. Large enough that a
# 900k-row partner is ~180 queries rather than 90,000, small enough that one
# batch is a few MB of Python objects.
BATCH_ROWS = 5000

# (column key, header in the file). The key must match a name selected by the
# events query below, or "venue"/"venue_city"/"venue_state"/"venue_country",
# which are filled in from the venue lookup.
DEFAULT_COLUMNS: List[Tuple[str, str]] = [
    ("id", "Event ID"),
    ("partner_event_id", "Partner Event ID"),
    ("title", "Title"),
    ("dates", "Start Date"),
    ("times", "Start Time"),
    ("enddates", "End Date"),
    ("endtimes", "End Time"),
    ("venue", "Venue"),
    ("venue_city", "City"),
    ("venue_state", "State"),
    ("venue_country", "Country"),
    ("price", "Price"),
    ("free_event", "Free Event"),
    ("booking_url", "Booking URL"),
    ("partner_url", "Partner URL"),
    ("website", "Website"),
    ("published", "Published"),
    ("cancelled", "Cancelled"),
    ("created", "Created"),
    ("modified", "Modified"),
]

# The events themselves. locid is selected so venues can be resolved separately;
# it is not written to the file unless a column asks for it.
#
# Paged by KEYSET (`id > last`), not by OFFSET. With OFFSET the server walks and
# discards every row before the window, so page N costs N pages of work and a
# 862,000-row partner like `fever` spends most of its time re-reading rows it
# has already written. `id > last` starts each batch where the previous one
# ended, so every page costs the same.
#
# The three parameters are bound in this order: partner, last id seen, batch
# size. A replacement query in config.yaml must keep them.
DEFAULT_EVENT_QUERY = (
    "SELECT id, partner_event_id, title, dates, times, enddates, endtimes, "
    "locid, price, free_event, booking_url, partner_url, website, published, "
    "cancelled, created, modified "
    "FROM jos_eventlist_events WHERE partner = %s AND id > %s "
    "ORDER BY id LIMIT %s"
)

# Candidate venue rows for a batch of ids. One flat scan; the choice of WHICH
# row wins is made in Python.
#
# It has to be one whole row, not MIN() per column: venue_details holds a row
# per partner submission (wid 92158 has 56 of them, spelling the country "US",
# "USA", "United States" and "United States of America"), so column-wise MIN
# returns a venue that never existed - the name from one submission beside the
# city from another.
#
# Choosing it with `id IN (SELECT MIN(id) ... GROUP BY wid)` is the obvious SQL
# and was 15x slower on real data (26.5s vs 1.7s for one partner): wid is not
# indexed, so the subquery makes the server scan the table twice per batch.
# Selecting id and taking the lowest per wid here costs one scan and no
# correctness.
DEFAULT_VENUE_QUERY = (
    "SELECT wid, id, name, city, state, country "
    "FROM venue_details WHERE wid IN ({placeholders})"
)

# Which records to include. "all" is the default because the point of the file
# is reconciliation against the partner's own sheet, and a row we hold but never
# published is exactly the kind of thing that reconciliation is looking for.
SCOPES = {
    "all": ("", "Every record we hold for this partner"),
    "live": (" AND published = '1' AND enddates >= CURRENT_DATE",
             "Published and not yet ended - today forward"),
    "unpublished": (" AND published <> '1'",
                    "Inserted but never went live"),
}


class ExportError(RuntimeError):
    """The export could not be produced, with a reason worth showing."""


def _scoped_query(sql: str, scope: str) -> str:
    """Apply the scope filter to the events query.

    Injected before ORDER BY rather than appended, because a WHERE clause after
    ORDER BY is a syntax error. The fragments come from SCOPES above and are
    never user input.
    """
    clause = SCOPES.get(scope, SCOPES["all"])[0]
    if not clause:
        return sql
    upper = sql.upper()
    cut = upper.find(" ORDER BY ")
    if cut == -1:
        cut = upper.find(" LIMIT ")
    if cut == -1:
        return sql + clause
    return sql[:cut] + clause + sql[cut:]


def _cell(value: Any) -> Any:
    """One value, rendered for a spreadsheet rather than for a parser."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    # MySQL TIME comes back as a timedelta; "68400 seconds" is not a start time.
    if hasattr(value, "total_seconds"):
        total = int(value.total_seconds())
        sign = "-" if total < 0 else ""
        total = abs(total)
        return f"{sign}{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"
    text = str(value)
    # MySQL's zero date is not a date, and Excel renders it as a red error.
    if text.startswith("0000-00-00"):
        return ""
    return text


def _resolve_venues(db: Dict[str, Any], venue_query: str,
                    locids: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    """Venue name/city/state/country for a batch of location ids.

    One grouped query per batch. A failure here is not fatal: an export with
    blank venue columns is far more use than no export at all, so it degrades
    rather than raising.
    """
    wanted = sorted({str(v).strip() for v in locids if str(v or "").strip().isdigit()})
    if not wanted:
        return {}
    sql = venue_query.format(placeholders=",".join(["%s"] * len(wanted)))
    try:
        rows = mysql.rows(db, sql, tuple(wanted))
    except Exception:
        return {}

    # Lowest id per wid wins, so all four fields come from one submission and a
    # re-run of the same export produces the same file.
    best: Dict[str, Any] = {}
    resolved: Dict[str, Dict[str, Any]] = {}
    for wid, row_id, name, city, state, country in rows:
        key = str(wid)
        if key in best and best[key] <= row_id:
            continue
        best[key] = row_id
        resolved[key] = {
            "venue": name, "venue_city": city,
            "venue_state": state, "venue_country": country,
        }
    return resolved


def build(
    partner: str,
    scope: str,
    db: Dict[str, Any],
    path: str,
    columns: Sequence[Tuple[str, str]] = DEFAULT_COLUMNS,
    event_query: str = DEFAULT_EVENT_QUERY,
    venue_query: str = DEFAULT_VENUE_QUERY,
    max_rows: Optional[int] = None,
    progress: Optional[Callable[[int], None]] = None,
) -> Dict[str, Any]:
    """Write every event for `partner` to `path` as CSV. Blocking - run in a thread.

    Returns {"rows": n, "size": bytes}. `progress` is called with the running
    row count every batch, which is what the status endpoint reports.
    """
    sql = _scoped_query(event_query, scope)
    # The same guard every other query goes through: this module must not be a
    # way around mysql.py's SELECT-only rule.
    mysql.assert_read_only(sql)

    needs_venue = any(key.startswith("venue") for key, _ in columns)
    # Resolved once, before any row is read: the mapping is a property of the
    # query, not of a batch, and deriving it inside the loop would leave it
    # undefined for an empty first page.
    field_index = _field_index(event_query)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    if "id" not in field_index:
        raise ExportError(
            "the events query must select `id` - it is the key each batch "
            "resumes from"
        )

    written = 0
    last_id = 0
    # utf-8-sig: Excel renders a venue or title with an accent as mojibake
    # without the BOM, and these files are opened in Excel by people.
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        writer.writerow([header for _, header in columns])

        while True:
            batch = mysql.rows(db, sql, (partner, last_id, BATCH_ROWS))
            if not batch:
                break

            venues: Dict[str, Dict[str, Any]] = {}
            if needs_venue and "locid" in field_index:
                venues = _resolve_venues(
                    db, venue_query, [row[field_index["locid"]] for row in batch]
                )

            for row in batch:
                record = {name: row[i] for name, i in field_index.items() if i < len(row)}
                if needs_venue:
                    record.update(venues.get(str(record.get("locid") or ""), {}))
                writer.writerow([_cell(record.get(key)) for key, _ in columns])
                written += 1

            last_id = batch[-1][field_index["id"]]
            if progress:
                progress(written)
            if max_rows and written >= max_rows:
                break
            if len(batch) < BATCH_ROWS:
                break

    return {"rows": written, "size": os.path.getsize(path)}


def _field_index(event_query: str) -> Dict[str, int]:
    """Map each selected column name to its position in the result tuple.

    Derived from the query's own SELECT list rather than hardcoded, so changing
    `queries.partner_events` in config.yaml cannot silently shift every value
    one column to the left.
    """
    upper = event_query.upper()
    start = upper.find("SELECT ") + len("SELECT ")
    end = upper.find(" FROM ")
    if end == -1:
        raise ExportError("the events query has no FROM clause")

    index: Dict[str, int] = {}
    for position, part in enumerate(event_query[start:end].split(",")):
        name = part.strip().split()[-1]        # handles "x AS y" and bare "x"
        name = name.rsplit(".", 1)[-1].strip("`")
        index[name] = position
    return index


def describe_scope(scope: str) -> str:
    return SCOPES.get(scope, SCOPES["all"])[1]
