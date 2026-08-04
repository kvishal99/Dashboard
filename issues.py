"""One definition of "something is wrong", used everywhere.

Before this, each page decided for itself what counted as a problem: the
partners table had its own "Problems only" rule, the jobs page ranked by a
different one, and the number on a summary tile agreed with neither. Anyone
reading two pages got two answers.

Everything that can be wrong is therefore enumerated here once. The Issues page
lists these, the partner cards count these, and the overview tiles sum these -
so "4 issues" on a card and the four rows you get after clicking it are the same
four things, by construction rather than by careful maintenance.

Each issue carries a severity, the partner or site it belongs to, a plain
sentence saying what is wrong, and where to go about it.
"""
from typing import Any, Dict, List, Optional
from urllib.parse import quote

# Severity drives sort order and colour. Three levels only: a fourth invites
# arguments about whether something is "medium", and the useful question on an
# ops dashboard is just "does this need me now, today, or never".
CRITICAL = "critical"   # red    - broken now, someone should act
WARNING = "warning"     # yellow - degraded or overdue, act today
INFO = "info"           # blue   - worth knowing, not a fault

SEVERITY_RANK = {CRITICAL: 0, WARNING: 1, INFO: 2}


def _issue(
    severity: str, kind: str, scope: str, subject: str,
    title: str, detail: str, link: Optional[str] = None,
    value: Optional[Any] = None, since: Optional[float] = None,
    last_run: Optional[float] = None, retry: Optional[str] = None,
    process: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        # Stable enough to key a UI row on, and to dedupe across a refresh.
        "id": f"{scope}:{subject}:{kind}",
        "severity": severity,
        "kind": kind,
        "scope": scope,
        "subject": subject,
        # What actually ran and went wrong, named as a process rather than as a
        # category. "Hourly count sweep" is something a person can go and look
        # at; "partner" is not.
        "process": process or subject,
        "title": title,
        "detail": detail,
        "link": link,
        "value": value,
        "since": since,
        # When the thing that produced this issue last ran, and the endpoint
        # that runs it again. `retry` is None wherever re-running is not a
        # meaningful response - a button that cannot help is worse than none.
        "last_run": last_run,
        "retry": retry,
        "rank": SEVERITY_RANK[severity],
    }


def for_partner(row: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Everything wrong with one partner.

    Ordered worst first. A partner with no issues returns an empty list, which
    is what makes `len(for_partner(row))` the number shown on its card.
    """
    found: List[Dict[str, Any]] = []
    name = row["name"]
    link = f"/partners/{name}"
    # Every partner issue shares these: the sweep is the process behind them,
    # collected_at is when it last ran, and re-counting is the one action that
    # can clear a stale or failed number.
    ran = row.get("collected_at")
    retry = f"/api/partners/{name}/refresh"

    # --- the counts themselves -------------------------------------------
    if row.get("ok") is False:
        found.append(_issue(
            CRITICAL, "query_failed", "partner", name,
            "Count query failed",
            row.get("error") or "The last count query against MySQL errored, so "
                                "every number for this partner is stale.",
            link, since=row.get("collected_at"),
            last_run=ran, retry=retry, process=f"{name} · hourly count sweep",
        ))
    elif row.get("ok") is None:
        found.append(_issue(
            INFO, "never_collected", "partner", name,
            "Never counted",
            "No counts have been collected for this partner yet. The MySQL "
            "sweep runs on the top of every hour.",
            link,
            last_run=ran, retry=retry, process=f"{name} · hourly count sweep",
        ))

    # --- is the ingest job still running? ---------------------------------
    state = row.get("job_state")
    days = row.get("job_days_since")
    if state == "stalled":
        # Stalled AND no cron entry is a different fix from stalled alone: the
        # job was removed, not broken, so it is called out as its own thing.
        removed = row.get("cron_status") == "missing"
        found.append(_issue(
            CRITICAL, "job_removed" if removed else "job_stalled", "partner", name,
            "Ingest job removed" if removed else "Ingest job stalled",
            (f"Nothing inserted for {days:.0f} days"
             if days is not None else "Nothing inserted for a long time")
            + (f", and no crontab entry exists on {row.get('server') or 'its server'} "
               "to run it - the job looks removed rather than broken."
               if removed else
               f", well past the {row.get('job_expected_days') or '?'}-day interval "
               f"its '{row.get('frequency') or 'unknown'}' schedule implies."),
            link, value=days,
            last_run=ran, retry=retry,
            process=f"{name} · ingest job ({row.get('frequency') or 'no schedule'})",
        ))
    elif state == "late":
        found.append(_issue(
            WARNING, "job_late", "partner", name,
            "Ingest job late",
            f"Overdue by {row.get('job_overdue_by') or 0:.0f} days against its "
            f"'{row.get('frequency') or 'unknown'}' schedule, but still inside "
            "the grace window.",
            link, value=row.get("job_overdue_by"),
            last_run=ran, retry=retry,
            process=f"{name} · ingest job ({row.get('frequency') or 'no schedule'})",
        ))
    elif state == "never":
        found.append(_issue(
            WARNING, "job_never", "partner", name,
            "Nothing ever inserted",
            "This partner has no records at all - the ingest has never "
            "successfully run.",
            link,
            last_run=ran, retry=retry, process=f"{name} · ingest job",
        ))

    # --- is anything actually live? ---------------------------------------
    if row.get("ok") and row.get("feed_total") and not row.get("db_future"):
        found.append(_issue(
            CRITICAL, "none_live", "partner", name,
            "Nothing live",
            f"We hold {row['feed_total']:,} records but none are published and "
            "still upcoming. The ingest is landing, but nothing is reaching the site.",
            link, value=row.get("feed_total"),
            last_run=ran, retry=retry, process=f"{name} · publishing",
        ))
    elif row.get("unpublished_pct") is not None and row["unpublished_pct"] >= 50:
        found.append(_issue(
            WARNING, "unpublished", "partner", name,
            "Half of records never published",
            f"{row.get('db_unpublished') or 0:,} of {row.get('feed_total') or 0:,} "
            f"records ({row['unpublished_pct']:.0f}%) inserted but never went live.",
            link, value=row.get("db_unpublished"),
            last_run=ran, retry=retry, process=f"{name} · publishing",
        ))

    # --- spreadsheet vs database ------------------------------------------
    comparison = row.get("comparison")
    if comparison and comparison.get("ok"):
        if comparison.get("missing"):
            found.append(_issue(
                CRITICAL if comparison["missing"] > (comparison.get("sheet_total") or 0) * 0.25
                else WARNING,
                "missing_tours", "partner", name,
                "Records missing from the database",
                f"{comparison['missing']:,} of {comparison.get('sheet_total') or 0:,} "
                f"rows in the partner spreadsheet have no match in our database "
                f"(matched on {comparison.get('strategy_label')}).",
                f"{link}#comparison", value=comparison["missing"],
                since=comparison.get("computed_at"),
                last_run=comparison.get("computed_at"),
                process=f"{name} · spreadsheet comparison",
            ))
        if comparison.get("extra"):
            found.append(_issue(
                WARNING, "extra_tours", "partner", name,
                "Extra records in the database",
                f"{comparison['extra']:,} records exist in our database with no "
                "row in the partner spreadsheet - usually withdrawn tours that "
                "were never removed on our side.",
                f"{link}#comparison", value=comparison["extra"],
                since=comparison.get("computed_at"),
                last_run=comparison.get("computed_at"),
                process=f"{name} · spreadsheet comparison",
            ))
        if comparison.get("reliable") is False:
            found.append(_issue(
                INFO, "weak_match", "partner", name,
                "Comparison key is weak",
                f"Only {comparison.get('match_rate', 0) * 100:.0f}% of rows matched "
                f"on {comparison.get('strategy_label')}. The missing/extra counts "
                "above are probably measuring a column mismatch, not real gaps.",
                f"{link}#comparison",
                last_run=comparison.get("computed_at"),
                process=f"{name} · spreadsheet comparison",
            ))

    found.sort(key=lambda i: i["rank"])
    return found


def for_site(row: Dict[str, Any]) -> List[Dict[str, Any]]:
    if row.get("ok") is False:
        return [_issue(
            CRITICAL, "site_down", "site", row["name"],
            "Website not responding",
            row.get("error") or f"The last check failed "
                                f"(HTTP {row.get('status_code') or 'no response'}).",
            "/processes#sites", since=row.get("checked_at"),
            last_run=row.get("checked_at"),
            retry=f"/api/sites/refresh?url={quote(row.get('url') or '', safe='')}",
            process=f"{row['name']} · website health check",
        )]
    return []


def for_server(row: Dict[str, Any]) -> List[Dict[str, Any]]:
    found = []
    if row.get("stale"):
        found.append(_issue(
            WARNING, "server_offline", "process", row["server_id"],
            "No heartbeat from server",
            f"This server's agent has not reported for {row.get('age_seconds', 0):.0f}s, "
            "so its process list is stale. Usually the agent or its tunnel is "
            "down rather than the box itself.",
            "/processes", since=row.get("last_updated"),
            last_run=row.get("last_updated"),
            process=f"{row['server_id']} · PM2 agent heartbeat",
        ))
    if row.get("errored_count"):
        found.append(_issue(
            CRITICAL, "process_errored", "process", row["server_id"],
            "PM2 processes errored",
            f"{row['errored_count']} of {row.get('total_count', 0)} processes on "
            "this server are in an errored state - they crashed or failed to start.",
            "/processes", value=row["errored_count"],
            last_run=row.get("last_updated"),
            process=f"{row['server_id']} · PM2 processes",
        ))
    return found


def collect(
    partners: List[Dict[str, Any]],
    sites: List[Dict[str, Any]],
    servers: List[Dict[str, Any]],
    db_ok: bool = True,
    db_detail: str = "",
) -> List[Dict[str, Any]]:
    """Every issue in the system, worst first.

    The single source the Issues page, the overview tiles and the per-partner
    counts all read from.
    """
    found: List[Dict[str, Any]] = []

    if not db_ok:
        found.append(_issue(
            CRITICAL, "db_unreachable", "system", "MySQL",
            "Database unreachable",
            db_detail or "The partner database is not answering, so no counts "
                         "can be collected and every number shown is stale.",
            "/settings",
        ))

    for partner in partners:
        found.extend(for_partner(partner))
    for site in sites:
        found.extend(for_site(site))
    for server in servers:
        found.extend(for_server(server))

    found.sort(key=lambda i: (i["rank"], -(i.get("value") or 0), i["subject"].lower()))
    return found


def summarise(found: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Counts by severity and by kind, for the tiles and the filter chips."""
    by_kind: Dict[str, int] = {}
    for issue in found:
        by_kind[issue["kind"]] = by_kind.get(issue["kind"], 0) + 1
    return {
        "total": len(found),
        "critical": sum(1 for i in found if i["severity"] == CRITICAL),
        "warning": sum(1 for i in found if i["severity"] == WARNING),
        "info": sum(1 for i in found if i["severity"] == INFO),
        "partners_affected": len({i["subject"] for i in found if i["scope"] == "partner"}),
        "by_kind": by_kind,
    }
