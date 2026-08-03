"""Comparing a partner's spreadsheet against what is actually in the database.

This answers the question the counts never could. `partner_counts` knows we hold
2,013 rows for bokun and the sheet says 85,000, but a gap of 82,987 is arithmetic,
not a comparison: two sides can hold the same number of records and still not be
the same records. Matching / missing / extra require looking at the rows.

Three things this module is deliberate about:

**It picks the match key by measurement, not by assumption.** A partner sheet
might identify a tour by product URL, by an id, or by nothing but its name.
Rather than requiring the operator to know which, every applicable strategy is
scored against the real data and the one that matches most rows wins. The chosen
strategy and its rate are returned, so a comparison built on a weak key is
visible as such instead of quietly reporting thousands of false "missing" rows.

**It refuses rather than truncates.** If a partner has more rows in MySQL than
`max_rows` allows, no comparison is produced at all. A partial diff would report
every unread row as missing - a wrong answer that looks like a real finding.

**Unmatched rows are kept, not just counted.** "You have 412 missing tours" is
where the question starts; the rows themselves are what someone acts on, so they
are stored and downloadable as CSV.
"""
import time
from typing import Any, Dict, List, Optional, Tuple

import mysql
import sheets

# How the two sides can be tied together, best first. Each entry is
# (name, label, how strong it is) - the label is what the UI shows.
STRATEGIES = [
    ("url", "Product URL", "exact"),
    ("external_id", "Partner ID", "exact"),
    ("title_date", "Title + start date", "strong"),
    ("title", "Title only", "weak"),
]

# A chosen strategy matching fewer than this share of the smaller side is
# reported as unreliable. It is not an error - a partner really can have 3%
# overlap - but a 2% match usually means the wrong column was mapped, and
# saying so beats presenting it as fact.
WEAK_MATCH_RATE = 0.10


def _extract_id(url: str) -> str:
    """The last meaningful path segment of a partner URL.

    Partner sheets very often carry the bare product id ('a-1029384') while our
    `partner_url` carries the whole URL that ends in it, so reducing the URL to
    its tail is what lets an id column match a URL column.
    """
    normalised = sheets.normalise_url(url)
    if not normalised:
        return ""
    segments = [s for s in normalised.split("/") if s]
    return segments[-1] if segments else ""


def _keys_for(row: Dict[str, Any]) -> Dict[str, str]:
    """Every key this row can be found by, keyed by strategy name.

    A row contributes to a strategy only when it actually has that field, so a
    blank cell never collides with another blank cell - which would otherwise
    match every empty-titled row to every other one.
    """
    keys: Dict[str, str] = {}

    url = sheets.normalise_url(row.get("url") or "")
    if url:
        keys["url"] = url

    # An id may be given as an id, or be embedded in the URL. Both are tried,
    # which is what lets a sheet of ids meet a database of URLs.
    ident = str(row.get("external_id") or "").strip().lower()
    if not ident and url:
        ident = _extract_id(url)
    if ident:
        keys["external_id"] = ident

    title = sheets.normalise_title(row.get("title"))
    if title:
        keys["title"] = title
        start = row.get("start_date")
        if start:
            keys["title_date"] = f"{title}|{start}"

    return keys


def _index(rows: List[Dict[str, Any]], strategy: str) -> Dict[str, List[int]]:
    """strategy key -> the row positions holding it. Lists, because both sides
    genuinely contain duplicates and collapsing them would hide that."""
    index: Dict[str, List[int]] = {}
    for position, row in enumerate(rows):
        key = row["_keys"].get(strategy)
        if key:
            index.setdefault(key, []).append(position)
    return index


def choose_strategy(
    sheet_rows: List[Dict[str, Any]], db_rows: List[Dict[str, Any]]
) -> Tuple[Optional[str], Dict[str, Dict[str, Any]]]:
    """Score every applicable strategy against the real rows; return the best.

    Scoring is done on the data rather than on which columns exist because a
    column being present says nothing about whether its values line up. A URL
    column full of values that appear nowhere in `partner_url` scores zero and
    correctly loses to matching on title.
    """
    scores: Dict[str, Dict[str, Any]] = {}
    best: Optional[str] = None

    for name, label, strength in STRATEGIES:
        sheet_index = _index(sheet_rows, name)
        db_index = _index(db_rows, name)
        if not sheet_index or not db_index:
            continue

        overlap = set(sheet_index) & set(db_index)
        matched_sheet_rows = sum(len(sheet_index[k]) for k in overlap)
        # Measured against the smaller side: a 200-row sheet fully present in a
        # 50,000-row database is a perfect key, and dividing by the big side
        # would score it at 0.4%.
        denominator = min(len(sheet_rows), len(db_rows)) or 1

        scores[name] = {
            "label": label,
            "strength": strength,
            "matched": matched_sheet_rows,
            "rate": round(matched_sheet_rows / denominator, 4),
            "sheet_coverage": round(sum(len(v) for v in sheet_index.values()) / (len(sheet_rows) or 1), 4),
            "db_coverage": round(sum(len(v) for v in db_index.values()) / (len(db_rows) or 1), 4),
        }
        if best is None or scores[name]["matched"] > scores[best]["matched"]:
            best = name

    return best, scores


def fetch_db_rows(
    database: Dict[str, Any], query: str, partner: str, max_rows: int
) -> Tuple[List[Dict[str, Any]], bool]:
    """Every record we hold for a partner, in the canonical shape.

    Returns (rows, truncated). `truncated` being True means the caller must not
    build a comparison: an unread row is indistinguishable from a missing one.
    The query is asked for one row more than the cap precisely so overflow is
    detectable rather than silently hit.
    """
    raw = mysql.rows(database, query, (partner, max_rows + 1))
    truncated = len(raw) > max_rows
    rows = []
    for record in raw[:max_rows]:
        # Column order is fixed by queries.partner_records in config.yaml:
        # id, title, dates, enddates, partner_url, published
        record_id, title, start, end, url, published = (list(record) + [None] * 6)[:6]
        rows.append({
            "db_id": record_id,
            "title": title or "",
            "start_date": str(start) if start else None,
            "end_date": str(end) if end else None,
            "url": url or "",
            "external_id": "",
            "published": int(published) if published is not None else None,
        })
    return rows, truncated


def compare(
    partner: str,
    sheet_rows: List[Dict[str, Any]],
    database: Dict[str, Any],
    query: str,
    max_rows: int = 100_000,
) -> Dict[str, Any]:
    """Diff a parsed sheet against the database. Never raises.

    A failure comes back as ok=False with the reason, because this runs behind a
    button in the UI and a traceback there tells the user nothing.
    """
    started = time.perf_counter()
    result: Dict[str, Any] = {
        "ok": False, "partner": partner, "error": None,
        "sheet_total": len(sheet_rows), "db_total": None,
        "matching": None, "missing": None, "extra": None,
        "strategy": None, "strategy_label": None, "strategy_strength": None,
        "match_rate": None, "reliable": None, "scores": {},
        "sheet_duplicates": 0, "db_duplicates": 0,
        "missing_rows": [], "extra_rows": [],
        "duration_ms": 0,
    }

    try:
        db_rows, truncated = fetch_db_rows(database, query, partner, max_rows)
        result["db_total"] = len(db_rows)

        if truncated:
            result["error"] = (
                f"{partner} holds more than {max_rows:,} records. Comparing a "
                f"partial read would report every unread row as missing, so no "
                f"comparison was produced. Raise app.max_compare_rows to proceed."
            )
            return result

        if not db_rows:
            result["error"] = f"no records in the database for {partner}"
            return result

        for row in sheet_rows:
            row["_keys"] = _keys_for(row)
        for row in db_rows:
            row["_keys"] = _keys_for(row)

        strategy, scores = choose_strategy(sheet_rows, db_rows)
        result["scores"] = scores

        if not strategy:
            result["error"] = (
                "no column in the sheet lines up with anything in the database - "
                "check that the tour name, id or URL column was mapped correctly"
            )
            return result

        label, strength = next((l, s) for n, l, s in STRATEGIES if n == strategy)
        sheet_index = _index(sheet_rows, strategy)
        db_index = _index(db_rows, strategy)
        sheet_keys, db_keys = set(sheet_index), set(db_index)
        both = sheet_keys & db_keys

        matched_rows = sum(len(sheet_index[k]) for k in both)
        missing_positions = [p for k in (sheet_keys - db_keys) for p in sheet_index[k]]
        extra_positions = [p for k in (db_keys - sheet_keys) for p in db_index[k]]

        # Rows that carry no key at all under the chosen strategy - a sheet row
        # with a blank title when matching on title. They are neither matching
        # nor confidently missing, so they are counted separately rather than
        # being swept into "missing".
        unkeyed_sheet = sum(1 for r in sheet_rows if strategy not in r["_keys"])
        unkeyed_db = sum(1 for r in db_rows if strategy not in r["_keys"])

        denominator = min(len(sheet_rows), len(db_rows)) or 1
        rate = matched_rows / denominator

        result.update({
            "ok": True,
            "matching": matched_rows,
            "missing": len(missing_positions),
            "extra": len(extra_positions),
            "strategy": strategy,
            "strategy_label": label,
            "strategy_strength": strength,
            "match_rate": round(rate, 4),
            "reliable": rate >= WEAK_MATCH_RATE,
            # A key appearing more than once on one side is worth surfacing:
            # duplicates in the sheet inflate its total, duplicates in the
            # database are usually a re-ingest that inserted twice.
            "sheet_duplicates": sum(len(v) - 1 for v in sheet_index.values() if len(v) > 1),
            "db_duplicates": sum(len(v) - 1 for v in db_index.values() if len(v) > 1),
            "unkeyed_sheet": unkeyed_sheet,
            "unkeyed_db": unkeyed_db,
            "missing_rows": [_present_sheet(sheet_rows[p]) for p in missing_positions],
            "extra_rows": [_present_db(db_rows[p]) for p in extra_positions],
        })
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"

    result["duration_ms"] = int((time.perf_counter() - started) * 1000)
    return result


def _present_sheet(row: Dict[str, Any]) -> Dict[str, Any]:
    """A sheet row trimmed to what gets stored and shown."""
    return {
        "title": row.get("title") or "",
        "external_id": row.get("external_id") or "",
        "start_date": row.get("start_date"),
        "end_date": row.get("end_date"),
        "venue": row.get("venue") or "",
        "url": row.get("url") or "",
    }


def _present_db(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "title": row.get("title") or "",
        "external_id": str(row.get("db_id") or ""),
        "start_date": row.get("start_date"),
        "end_date": row.get("end_date"),
        "venue": "",
        "url": row.get("url") or "",
        "published": row.get("published"),
    }
