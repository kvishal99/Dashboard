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
    last_run: Optional[float] = None,
    process: Optional[str] = None, action: str = "",
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
        # What to actually do about it, in one short sentence. An issue list
        # that only names problems leaves everyone guessing at the next step,
        # which is what made the old page hard to act on.
        "action": action,
        "link": link,
        "value": value,
        "since": since,
        # When the thing that produced this issue last ran. There is no
        # "run it again" alongside it: this dashboard reports on the estate
        # rather than operating it, and the only honest next step is the
        # sentence in `action`.
        "last_run": last_run,
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

    # --- the counts themselves -------------------------------------------
    if row.get("ok") is False:
        found.append(_issue(
            CRITICAL, "query_failed", "partner", name,
            "Could not read the database",
            row.get("error") or "The last count query against MySQL errored, so "
                                "every number for this partner is stale.",
            link, since=row.get("collected_at"),
            last_run=ran, process=f"{name} · hourly count sweep",
            action="Check the database connection, then re-run the count.",
        ))
    elif row.get("ok") is None:
        found.append(_issue(
            INFO, "never_collected", "partner", name,
            "Not counted yet",
            "No counts have been collected for this partner yet. The MySQL "
            "sweep runs on the top of every hour.",
            link,
            last_run=ran, process=f"{name} · hourly count sweep",
            action="Nothing to do - the next hourly sweep will pick it up.",
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
            "Stopped importing - nothing scheduled to run it" if removed
            else "Stopped importing events",
            (f"Nothing inserted for {days:.0f} days"
             if days is not None else "Nothing inserted for a long time")
            + (f", and no crontab entry exists on {row.get('server') or 'its server'} "
               "to run it - the job looks removed rather than broken."
               if removed else
               f", well past the {row.get('job_expected_days') or '?'}-day interval "
               f"its '{row.get('frequency') or 'unknown'}' schedule implies."),
            link, value=days,
            last_run=ran,
            process=f"{name} · ingest job ({row.get('frequency') or 'no schedule'})",
            action=("Add a crontab entry on " + (row.get("server") or "its server")
                    + " - the job is not scheduled anywhere.") if removed
                   else "Check the ingest script on " + (row.get("server") or "its server")
                    + " and its log for errors.",
        ))
    elif state == "late":
        found.append(_issue(
            WARNING, "job_late", "partner", name,
            "Import is overdue",
            f"Overdue by {row.get('job_overdue_by') or 0:.0f} days against its "
            f"'{row.get('frequency') or 'unknown'}' schedule, but still inside "
            "the grace window.",
            link, value=row.get("job_overdue_by"),
            last_run=ran,
            process=f"{name} · ingest job ({row.get('frequency') or 'no schedule'})",
            action="Watch it - if it does not catch up by the next run, check the script.",
        ))
    elif state == "never":
        found.append(_issue(
            WARNING, "job_never", "partner", name,
            "Never imported anything",
            "This partner has no records at all - the ingest has never "
            "successfully run.",
            link,
            last_run=ran, process=f"{name} · ingest job",
            action="Confirm this partner is meant to be live, then check its script.",
        ))

    # --- is anything actually live? ---------------------------------------
    if row.get("ok") and row.get("feed_total") and not row.get("db_future"):
        found.append(_issue(
            CRITICAL, "none_live", "partner", name,
            "No events showing on the site",
            f"We hold {row['feed_total']:,} records but none are published and "
            "still upcoming. The ingest is landing, but nothing is reaching the site.",
            link, value=row.get("feed_total"),
            last_run=ran, process=f"{name} · publishing",
            action="The import works but nothing is published - check the publish step.",
        ))
    elif row.get("unpublished_pct") is not None and row["unpublished_pct"] >= 50:
        found.append(_issue(
            WARNING, "unpublished", "partner", name,
            "Most events are not visible on the site",
            f"{row.get('db_unpublished') or 0:,} of {row.get('feed_total') or 0:,} "
            f"records ({row['unpublished_pct']:.0f}%) inserted but never went live.",
            link, value=row.get("db_unpublished"),
            last_run=ran, process=f"{name} · publishing",
            action="Check why these events were inserted but never published.",
        ))

    # The spreadsheet comparison used to raise three more issues here
    # (missing_tours, extra_tours, weak_match). That feature was removed from
    # the UI, and an issue nobody can open a page to act on is noise.

    found.sort(key=lambda i: i["rank"])
    return found


def for_site(row: Dict[str, Any]) -> List[Dict[str, Any]]:
    if row.get("ok") is False:
        return [_issue(
            CRITICAL, "site_down", "site", row["name"],
            "Website is down",
            row.get("error") or f"The last check failed "
                                f"(HTTP {row.get('status_code') or 'no response'}).",
            "/processes#sites", since=row.get("checked_at"),
            last_run=row.get("checked_at"),
            process=f"{row['name']} · website health check",
            action="Open the site. If it loads, the check may need its expected status updating.",
        )]
    return []


def for_server(row: Dict[str, Any]) -> List[Dict[str, Any]]:
    found = []
    if row.get("stale"):
        found.append(_issue(
            WARNING, "server_offline", "process", row["server_id"],
            "Server stopped reporting",
            f"This server's agent has not reported for {row.get('age_seconds', 0):.0f}s, "
            "so its process list is stale. Usually the agent or its tunnel is "
            "down rather than the box itself.",
            "/processes", since=row.get("last_updated"),
            last_run=row.get("last_updated"),
            process=f"{row['server_id']} · PM2 agent heartbeat",
            action="Restart the agent on that server, or check the tunnel is up.",
        ))
    if row.get("errored_count"):
        found.append(_issue(
            CRITICAL, "process_errored", "process", row["server_id"],
            "Processes have crashed",
            f"{row['errored_count']} of {row.get('total_count', 0)} processes on "
            "this server are in an errored state - they crashed or failed to start.",
            "/processes", value=row["errored_count"],
            last_run=row.get("last_updated"),
            process=f"{row['server_id']} · PM2 processes",
            action="Run `pm2 list` on that server and restart the errored processes.",
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
            "Database is unreachable",
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
