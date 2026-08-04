"""Ops Dashboard - partner ingestion monitoring, spreadsheet comparison,
website health and PM2 processes.

Run with:  ./venv/bin/python dashboard.py

The shape of the API mirrors the shape of the UI, and both follow one rule: the
main dashboard shows a summary and nothing else. `/api/overview` returns one
small card per partner - name, status, total, last updated, issue count - and
never the detail behind them. Everything heavier (count history, the comparison
row lists, the crontab inventory) sits behind a per-partner route that is only
called when someone actually opens that partner.

That is why the overview stays fast with 112 partners, and why adding a 113th
costs the front page nothing.
"""
import asyncio
import os
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import quote

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import (FileResponse, JSONResponse, Response,
                               StreamingResponse)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

import activity as activity_mod
import applog
import compare as compare_mod
import cron_collect
import cron_parse
import event_export
import exports
import issues as issues_mod
import jobs as jobs_mod
import mysql
import sheets
from auth import BasicAuthMiddleware
from config import BASE_DIR, load_config
from scheduler import Scheduler
from store import Store

config = load_config()
store = Store(config.db_path)
scheduler = Scheduler(config, store)

# Latest PM2 heartbeat per server. Kept in memory on purpose - it arrives every
# few seconds and only the newest report is ever displayed.
PM2_STORE: Dict[str, Dict[str, Any]] = {}


# Set OPS_DISABLE_SCHEDULER=1 to serve the UI without running any background
# jobs - useful for working on the front end, or when MySQL is unreachable and
# you don't want a wall of failed collections.
SCHEDULER_ENABLED = os.environ.get("OPS_DISABLE_SCHEDULER", "") not in ("1", "true", "yes")

# The sections in the sidebar, in order. Declared here rather than in the
# template so every page agrees on what exists and what is current, and so the
# Knowledge Base can be switched on later by flipping `enabled` - no template,
# route or stylesheet has to change for it to appear.
#
# Partners sits at the top and is where the work happens: you pick a partner and
# see that partner's jobs, logs, files and issues, and nothing about anyone
# else. The sections below it are the cross-partner views that genuinely have to
# span everything - what is wrong right now, whether the machinery is running,
# and how it was configured.
#
# There is deliberately no global Logs section. It showed every line the process
# wrote for every partner at once, so answering "what happened to WCities?"
# meant reading past a hundred lines about somebody else. Those lines are now on
# the partner's own Logs tab, and the log FILES are still downloadable from
# Downloads - see activity.py.
NAV: List[Dict[str, Any]] = [
    {"key": "overview", "label": "Overview", "href": "/", "icon": "grid"},
    {"key": "partners", "label": "Partners", "href": "/partners", "icon": "users"},
    {"key": "issues", "label": "Issues", "href": "/issues", "icon": "alert", "badge": "issues"},
    {"key": "processes", "label": "Processes", "href": "/processes", "icon": "activity"},
    {"key": "downloads", "label": "Downloads", "href": "/downloads", "icon": "download"},
    {"key": "reports", "label": "Reports", "href": "/reports", "icon": "chart"},
    {"key": "settings", "label": "Settings", "href": "/settings", "icon": "cog"},
    # Ships disabled: the nav slot, the active-state handling and the sidebar
    # spacing all exist now, so building the module later is a new template and
    # nothing else. Designing the shell around seven items and adding an eighth
    # afterwards is what forces a redesign.
    {"key": "kb", "label": "Knowledge Base", "href": "/knowledge-base",
     "icon": "book", "enabled": False, "note": "Coming soon"},
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Timestamp every log line and mirror it to the file the partner Logs tabs
    # read.
    #
    # This has to happen HERE rather than at import time: uvicorn installs its
    # own logging configuration when the server starts, which is after this
    # module is imported and would replace any formatter set earlier. Startup
    # runs after that, so this is the first point at which the format sticks.
    applog.configure(config.log_file, config.log_level)

    # An export that was running when the process stopped has no process behind
    # it any more. Left alone it would sit at "running" forever, and its partner
    # could never start another one.
    orphaned = store.reset_running_exports()
    if orphaned:
        print(f"[exports] marked {orphaned} interrupted export(s) as failed")
    stranded = store.reset_running_fetches()
    if stranded:
        print(f"[fetch] marked {stranded} interrupted transfer(s) as failed")

    if SCHEDULER_ENABLED:
        scheduler.start()
    yield
    await scheduler.stop()


app = FastAPI(title="Ops Dashboard", lifespan=lifespan)

# The .htaccess equivalent: a browser login in front of every page and API
# route. Only added when a password is configured, so a local checkout without
# .env still runs; see auth.py for what stays exempt and why.
if config.auth_enabled:
    app.add_middleware(
        BasicAuthMiddleware, username=config.auth_user, password=config.auth_password
    )
else:
    print("[auth] OPS_AUTH_PASSWORD is not set - dashboard is UNPROTECTED")

app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))


def page(request: Request, template: str, active: str, **context: Any) -> Any:
    """Render a page with the shell context every template needs."""
    return templates.TemplateResponse(
        request, template, {"active": active, "nav": NAV, **context}
    )


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def _pct(part: Optional[int], whole: Optional[int]) -> Optional[float]:
    if not whole or part is None:
        return None
    return round(100.0 * part / whole, 2)


def _iso(ts: Optional[float]) -> Optional[str]:
    if not ts:
        return None
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def known_partners() -> List[str]:
    """Partners we have counts for - discovered from the database, not configured."""
    return sorted(store.latest_counts().keys(), key=str.lower)


def known_partner_names() -> set:
    """Every partner name worth matching a crontab path against.

    Both sources, because neither is complete on its own: the status sheet is
    missing partners that are live in MySQL, and MySQL is missing partners that
    have a cron job but no rows (fandango, ticketsnow and reservix all have
    scripts on disk and nothing in the database). Matching against only
    partner_meta - which is what this used to pass - threw away the 120
    partners the dashboard had already discovered.
    """
    return set(store.latest_counts().keys()) | set(config.partner_meta.keys())


def build_partner_rows(with_comparison: bool = True) -> List[Dict[str, Any]]:
    """Discovered partners joined with their latest counts, plus deltas."""
    latest = store.latest_counts()
    previous = store.previous_counts()
    feeds = store.latest_feed_counts()
    comparisons = store.latest_comparisons() if with_comparison else {}

    rows = []
    for name in sorted(latest.keys(), key=str.lower):
        p = config.meta_for(name)
        cur = latest.get(name)
        prev = previous.get(name)
        feed = feeds.get(name)

        feed_total = cur["feed_total"] if cur else None
        db_future = cur["db_future"] if cur else None
        db_past = cur["db_past"] if cur else None

        # Inserted but never went live. Derived, so rows collected before
        # db_past existed stay None rather than reporting a misleading 0.
        unpublished = (
            max(feed_total - db_future - db_past, 0)
            if None not in (feed_total, db_future, db_past)
            else None
        )
        # Kept for the older "everything that isn't live" view.
        not_live = (
            max(feed_total - db_future, 0)
            if feed_total is not None and db_future is not None
            else None
        )

        rows.append(
            {
                "name": name,
                "server": p.get("server"),
                "frequency": p.get("frequency"),
                "note": p.get("note"),
                "path": p.get("path"),
                "feed_total": feed_total,
                "db_future": db_future,
                "db_past": db_past,
                "db_unpublished": unpublished,
                "past_or_unpublished": not_live,
                "live_pct": _pct(db_future, feed_total),
                "unpublished_pct": _pct(unpublished, feed_total),
                # What the PARTNER has. None until something reports it - shown
                # as a dash rather than guessed, because it is not in our MySQL.
                "partner_feed_count": feed["feed_count"] if feed else None,
                "feed_source": feed["source"] if feed else None,
                "feed_reported_at": feed["reported_at"] if feed else None,
                # Of everything the partner offered, how much did we take?
                "ingested_pct": (
                    _pct(feed_total, feed["feed_count"])
                    if feed and feed.get("feed_count") else None
                ),
                "missing_from_db": (
                    max(feed["feed_count"] - feed_total, 0)
                    if feed and feed.get("feed_count") and feed_total is not None
                    else None
                ),
                # Job freshness: MAX(created) vs the cron cadence in the sheet.
                "last_created": cur["last_created"] if cur else None,
                **{
                    f"job_{k}": v
                    for k, v in jobs_mod.assess(
                        cur["last_created"] if cur else None,
                        p.get("frequency"),
                        note=p.get("note"),
                        live_events=db_future,
                    ).items()
                },
                "delta_future": (
                    db_future - prev["db_future"]
                    if cur and prev and db_future is not None and prev["db_future"] is not None
                    else None
                ),
                "ok": bool(cur["ok"]) if cur else None,
                "error": cur["error"] if cur else None,
                "collected_at": cur["collected_at"] if cur else None,
                "collected_at_iso": _iso(cur["collected_at"] if cur else None),
                "duration_ms": cur["duration_ms"] if cur else None,
                # The record-level spreadsheet diff, when one has been run.
                "comparison": comparisons.get(name),
            }
        )
    return rows


# The cron cross-reference reads every collected crontab line and is the same
# for every partner, so it is computed once per call rather than per row. Cached
# briefly because the overview polls every few seconds while crontabs arrive
# every six hours - re-deriving it on each poll is pure waste.
_CRON_CACHE: Dict[str, Any] = {"at": 0.0, "by_partner": {}, "servers": set()}
_CRON_TTL = 30.0


def cron_index() -> Tuple[Dict[str, List[Dict[str, Any]]], set]:
    now = time.time()
    if now - _CRON_CACHE["at"] > _CRON_TTL:
        by_partner: Dict[str, List[Dict[str, Any]]] = {}
        for entry in store.cron_jobs():
            if entry["partner"]:
                by_partner.setdefault(entry["partner"].lower(), []).append(entry)
        _CRON_CACHE.update({
            "at": now,
            "by_partner": by_partner,
            "servers": {s["server"] for s in store.cron_servers()},
        })
    return _CRON_CACHE["by_partner"], _CRON_CACHE["servers"]


def annotate_cron(rows: List[Dict[str, Any]]) -> None:
    """Attach cron entry status to each partner row, in place.

    "Stopped inserting" alone means the job is broken. "Stopped inserting AND
    has no cron entry" means it was removed - a different fix - which neither
    fact establishes on its own.
    """
    by_partner, collected_servers = cron_index()
    for row in rows:
        entries = by_partner.get(row["name"].lower(), [])
        active = [e for e in entries if not e["disabled"]]
        if active:
            status = "found"
        elif entries:
            # Present but commented out - a deliberate disable, worth naming
            # separately from a cron that isn't there at all.
            status = "disabled"
        elif not collected_servers or not row.get("server"):
            status = "unknown"
        elif row["server"] not in collected_servers:
            # We never looked at that box, so absence proves nothing.
            status = "unknown"
        else:
            status = "missing"
        row["cron_status"] = status
        row["cron_count"] = len(entries)
        row["cron_schedule"] = (active or entries or [{}])[0].get("schedule_human")


def partner_status(row: Dict[str, Any]) -> str:
    """The one word the card shows: running | success | warning | failed.

    Derived from the issues on the row rather than from a second set of rules,
    so a card can never read "Success" beside a red issue count.
    """
    if scheduler.jobs["counts"].running and row.get("collected_at") is None:
        return "running"
    found = row.get("issues") or []
    if any(i["severity"] == issues_mod.CRITICAL for i in found):
        return "failed"
    if any(i["severity"] == issues_mod.WARNING for i in found):
        return "warning"
    return "success"


def build_site_rows() -> List[Dict[str, Any]]:
    checks = store.latest_checks()
    day_ago = time.time() - 86400
    rows = []
    for site in config.websites:
        check = checks.get(site["url"])
        uptime = store.uptime_since(site["url"], day_ago)
        rows.append(
            {
                "name": site["name"],
                "url": site["url"],
                "expect_status": site.get("expect_status", 200),
                "ok": bool(check["ok"]) if check else None,
                "status_code": check["status_code"] if check else None,
                "latency_ms": check["latency_ms"] if check else None,
                "error": check["error"] if check else None,
                "checked_at": check["checked_at"] if check else None,
                "checked_at_iso": _iso(check["checked_at"] if check else None),
                "uptime_24h": uptime["uptime_pct"],
                "checks_24h": uptime["checks"],
                "avg_latency_24h": uptime["avg_latency_ms"],
            }
        )
    # Down sites first, then never-checked, then healthy.
    rows.sort(key=lambda r: (r["ok"] is True, r["ok"] is None, r["name"]))
    return rows


def build_server_rows() -> List[Dict[str, Any]]:
    now = time.time()
    servers = []
    for srv in PM2_STORE.values():
        age = now - srv["last_updated"]
        procs = srv["processes"]
        servers.append(
            {
                **srv,
                "stale": age > config.pm2_stale_seconds,
                "age_seconds": round(age, 1),
                "online_count": sum(1 for p in procs if p.get("status") == "online"),
                "errored_count": sum(1 for p in procs if p.get("status") == "errored"),
                "total_count": len(procs),
            }
        )
    servers.sort(key=lambda s: (not s["stale"] and s["errored_count"] == 0, s["server_id"]))
    return servers


def full_state() -> Dict[str, Any]:
    """Partners, sites, servers and every issue across them.

    One function because the issue list has to be derived from exactly the rows
    the pages display - computing them separately is how a tile and a table end
    up disagreeing.
    """
    partners = build_partner_rows()
    annotate_cron(partners)
    sites = build_site_rows()
    servers = build_server_rows()

    found = issues_mod.collect(partners, sites, servers)

    by_subject: Dict[str, List[Dict[str, Any]]] = {}
    for issue in found:
        if issue["scope"] == "partner":
            by_subject.setdefault(issue["subject"], []).append(issue)
    for row in partners:
        row["issues"] = by_subject.get(row["name"], [])
        row["issue_count"] = len(row["issues"])
        row["status"] = partner_status(row)

    return {"partners": partners, "sites": sites, "servers": servers, "issues": found}


def _partner_row(partner_name: str) -> Dict[str, Any]:
    """One partner's full row, or 404. The per-partner routes all start here."""
    for row in full_state()["partners"]:
        if row["name"] == partner_name:
            return row
    raise HTTPException(status_code=404, detail=f"unknown partner: {partner_name}")


def build_summary(partners: List[Dict[str, Any]]) -> Dict[str, Any]:
    sites = build_site_rows()
    servers = build_server_rows()
    feed = [p["feed_total"] for p in partners if p["feed_total"] is not None]
    live = [p["db_future"] for p in partners if p["db_future"] is not None]
    past = [p["db_past"] for p in partners if p["db_past"] is not None]
    unpub = [p["db_unpublished"] for p in partners if p["db_unpublished"] is not None]
    feed_sum, live_sum = sum(feed), sum(live)
    return {
        "partners": len(partners),
        "partners_with_data": len(feed),
        "feed_total": feed_sum,
        "db_future": live_sum,
        "db_past": sum(past),
        "db_unpublished": sum(unpub),
        "past_or_unpublished": max(feed_sum - live_sum, 0),
        "live_pct": _pct(live_sum, feed_sum),
        "unpublished_pct": _pct(sum(unpub), feed_sum),
        "query_errors": sum(1 for p in partners if p["ok"] is False),
        "never_collected": sum(1 for p in partners if p["ok"] is None),
        "sites_total": len(sites),
        "sites_down": sum(1 for s in sites if s["ok"] is False),
        "servers_total": len(servers),
        "servers_stale": sum(1 for s in servers if s["stale"]),
        "processes_total": sum(s["total_count"] for s in servers),
        "processes_errored": sum(s["errored_count"] for s in servers),
    }


# The fields a partner card needs, and nothing else. Sending the full row would
# be roughly six times the payload for data the card cannot display.
CARD_FIELDS = (
    "name", "status", "issue_count", "feed_total", "db_future", "live_pct",
    "collected_at", "last_created", "job_state", "server", "frequency",
    # Days since the partner last had anything inserted, computed here from
    # MAX(created). The list shows "last run" from this rather than re-parsing
    # last_created in the browser: it is a MySQL datetime in server-local time,
    # and Date.parse would have to guess a timezone to turn it into an age.
    "job_days_since",
)


def to_card(row: Dict[str, Any]) -> Dict[str, Any]:
    card = {key: row.get(key) for key in CARD_FIELDS}
    card["worst_issue"] = row["issues"][0]["title"] if row.get("issues") else None
    return card


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


@app.get("/")
async def page_overview(request: Request):
    return page(request, "overview.html", "overview")


@app.get("/partners")
async def page_partners(request: Request):
    return page(request, "partners.html", "partners", selected=None)


@app.get("/partners/{partner_name}")
async def page_partner_detail(request: Request, partner_name: str):
    """The same workspace, with one partner already selected.

    A partner is a URL rather than only a click, because links to a partner are
    pasted into tickets and chat, and the Issues page links straight to the tab
    that explains each issue.
    """
    if partner_name not in store.latest_counts():
        raise HTTPException(status_code=404, detail=f"unknown partner: {partner_name}")
    return page(request, "partners.html", "partners", selected=partner_name)


@app.get("/processes")
async def page_processes(request: Request):
    return page(request, "processes.html", "processes")


@app.get("/issues")
async def page_issues(request: Request):
    return page(request, "issues.html", "issues")


# There is no /logs page any more - the global log view was removed because it
# mixed every partner together. The log API and the log file downloads below are
# deliberately kept: they still back the partner Logs tabs (activity.py reads
# through applog) and the Downloads page, and removing working endpoints that
# nothing asked to lose would be a different change from redesigning the UI.


@app.get("/downloads")
async def page_downloads(request: Request):
    return page(request, "downloads.html", "downloads")


@app.get("/reports")
async def page_reports(request: Request):
    return page(request, "reports.html", "reports")


@app.get("/settings")
async def page_settings(request: Request):
    return page(request, "settings.html", "settings")


# ---------------------------------------------------------------------------
# API - the overview
# ---------------------------------------------------------------------------


@app.get("/api/overview")
async def api_overview():
    """Everything the main dashboard shows, and nothing more.

    Deliberately summary-only: one small card per partner plus the headline
    counts. No history, no comparison rows, no crontab lines - those are fetched
    when a partner is opened.
    """
    state = full_state()
    partners = state["partners"]
    summary = build_summary(partners)
    issue_summary = issues_mod.summarise(state["issues"])

    by_status: Dict[str, int] = {}
    for row in partners:
        by_status[row["status"]] = by_status.get(row["status"], 0) + 1

    watched = set(store.watchlist())
    cards = [to_card(row) for row in partners]
    for card in cards:
        card["watched"] = card["name"] in watched

    return {
        "cards": cards,
        "watchlist": sorted(watched),
        "summary": {
            **summary,
            "issues": issue_summary,
            "by_status": by_status,
            "healthy": by_status.get("success", 0),
        },
        "jobs": scheduler.status(),
        "now": time.time(),
    }


# ---------------------------------------------------------------------------
# API - the watchlist
#
# Which partners this team actually looks after. 120 in a list is too many to
# scan when eight of them are yours, so the partner list can be narrowed to
# these - see store.partner_watchlist for why it is stored server-side.
# ---------------------------------------------------------------------------


@app.get("/api/watchlist")
async def api_watchlist():
    return {"watchlist": store.watchlist()}


@app.post("/api/watchlist/{partner_name}")
async def api_watch(partner_name: str):
    _require_partner(partner_name)
    store.watch(partner_name)
    return {"status": "ok", "watchlist": store.watchlist()}


@app.delete("/api/watchlist/{partner_name}")
async def api_unwatch(partner_name: str):
    # Deliberately not _require_partner: a partner that has since dropped out of
    # the database must still be removable, or the list can never be cleaned up.
    store.unwatch(partner_name)
    return {"status": "ok", "watchlist": store.watchlist()}


class WatchlistUpdate(BaseModel):
    partners: List[str]


@app.put("/api/watchlist")
async def api_set_watchlist(update: WatchlistUpdate):
    store.set_watchlist(update.partners)
    return {"status": "ok", "watchlist": store.watchlist()}


# ---------------------------------------------------------------------------
# API - partners
# ---------------------------------------------------------------------------


@app.get("/api/partners")
async def api_partners():
    state = full_state()
    rows = state["partners"]
    return {
        "partners": rows,
        "summary": build_summary(rows),
        "issues": issues_mod.summarise(state["issues"]),
        "jobs": scheduler.status(),
        "now": time.time(),
    }


@app.get("/api/partners/{partner_name}")
async def api_partner(partner_name: str):
    """One partner, in full. Called only when a partner is actually opened."""
    for row in full_state()["partners"]:
        if row["name"] == partner_name:
            row["uploads"] = store.uploads(partner_name, limit=20)
            return {"partner": row, "jobs": scheduler.status(), "now": time.time()}
    raise HTTPException(status_code=404, detail=f"unknown partner: {partner_name}")


@app.get("/api/partners/{partner_name}/history")
async def api_partner_history(partner_name: str, limit: int = Query(60, ge=1, le=500)):
    if partner_name not in store.latest_counts():
        raise HTTPException(status_code=404, detail=f"unknown partner: {partner_name}")
    rows = store.counts_history(partner_name, limit)
    for row in rows:
        row["collected_at_iso"] = _iso(row["collected_at"])
        row["db_unpublished"] = (
            max(row["feed_total"] - row["db_future"] - row["db_past"], 0)
            if None not in (row["feed_total"], row["db_future"], row["db_past"])
            else None
        )
    return {"history": rows}


@app.post("/api/partners/{partner_name}/refresh")
async def api_partner_refresh(partner_name: str):
    if partner_name not in store.latest_counts():
        raise HTTPException(status_code=404, detail=f"unknown partner: {partner_name}")
    return await scheduler.run_counts(partner_name=partner_name)


@app.post("/api/counts/refresh")
async def api_counts_refresh():
    return await scheduler.run_counts()


class FeedReport(BaseModel):
    """How many records the PARTNER actually had on their side.

    This number exists nowhere in our MySQL - only the thing that read the feed
    knows it. Post it from the end of an ingest run, from a feed-file counter,
    or by hand.
    """
    partner: str
    feed_count: Optional[int] = Field(default=None, ge=0)
    inserted: Optional[int] = Field(default=None, ge=0)
    source: str = Field(default="script", pattern="^(script|file|api|sheet|manual)$")
    note: Optional[str] = None


@app.post("/api/partners/feed-count")
async def api_report_feed_count(
    report: FeedReport, x_agent_secret: str = Header(None)
):
    if x_agent_secret != config.agent_secret:
        raise HTTPException(status_code=401, detail="Invalid token")
    store.record_feed_count(
        partner=report.partner,
        feed_count=report.feed_count,
        inserted=report.inserted,
        source=report.source,
        note=report.note,
    )
    return {"status": "ok", "partner": report.partner, "feed_count": report.feed_count}


@app.get("/api/partners/{partner_name}/feed-history")
async def api_feed_history(partner_name: str, limit: int = Query(60, ge=1, le=500)):
    return {"history": store.feed_history(partner_name, limit)}


# ---------------------------------------------------------------------------
# API - one partner's jobs, logs and files
#
# These three are what make the dashboard partner-centric. Each answers its
# question for ONE partner and returns nothing about any other, so selecting a
# partner genuinely changes what is on screen rather than filtering a page that
# has already loaded everything.
# ---------------------------------------------------------------------------


@app.get("/api/partners/{partner_name}/jobs")
async def api_partner_jobs_detail(partner_name: str):
    """This partner's ingest job and its crontab lines, grouped by category.

    The categories matter because a partner's rows are not all the same kind of
    work: an insert job going quiet is an outage, while a report generator going
    quiet usually is not.
    """
    _require_partner(partner_name)

    rows = store.cron_jobs_for_partner(partner_name)
    now = time.time()
    for row in rows:
        row["log_age_days"] = (
            round((now - row["log_mtime"]) / 86400.0, 1) if row["log_mtime"] else None
        )
        row["disabled"] = bool(row["disabled"])
        # Rows stored before categories existed carry none. Derived here rather
        # than defaulted to "other", which filed every pre-existing insert job
        # under Other - the exact mislabelling this feature exists to remove.
        if not row.get("category"):
            row["category"] = cron_parse.categorise(
                row.get("script"), row.get("command") or "", row.get("name") or ""
            )
        row["category_label"] = cron_parse.CATEGORY_LABELS.get(
            row["category"], "Other"
        )

    groups: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(row["category"], []).append(row)

    # Declared order, not dictionary order, so the sections do not reshuffle
    # between two partners.
    categories = [
        {"key": key, "label": label, "detail": detail, "jobs": groups.get(key, [])}
        for key, label, detail in cron_parse.CATEGORIES
        if groups.get(key)
    ]

    partner_row = _partner_row(partner_name)
    return {
        "partner": partner_name,
        "categories": categories,
        "summary": {
            "total": len(rows),
            "active": sum(1 for r in rows if not r["disabled"]),
            "disabled": sum(1 for r in rows if r["disabled"]),
        },
        # The ingest-freshness verdict, which is derived from MAX(created)
        # rather than from the crontab.
        "ingest": {
            key: partner_row.get(key)
            for key in ("job_state", "job_days_since", "job_expected_days",
                        "job_overdue_by", "cron_status", "cron_schedule",
                        "frequency", "server", "last_created")
        },
        "now": now,
    }


@app.get("/api/partners/{partner_name}/logs")
async def api_partner_logs(partner_name: str, limit: int = Query(120, ge=1, le=500)):
    """This partner's process log, newest first.

    Assembled from what the dashboard already recorded about this partner -
    collections, feed reports, uploads, comparisons, exports, cron output and
    the app log lines that name it. See activity.py for why each source is
    included and what it can and cannot claim.
    """
    _require_partner(partner_name)

    entries = activity_mod.build(
        partner=partner_name,
        counts=store.counts_history(partner_name, limit=60),
        feed_reports=store.feed_history(partner_name, limit=20),
        uploads=store.uploads(partner_name, limit=20),
        comparisons=store.comparison_history(partner_name, limit=20),
        exports=store.exports(partner_name, limit=20),
        cron_rows=store.cron_jobs_for_partner(partner_name),
        log_path=config.log_file,
        limit=limit,
    )
    for entry in entries:
        entry["time"] = _iso(entry["ts"])
    return {
        "partner": partner_name,
        "entries": entries,
        "summary": activity_mod.summarise(entries),
        "now": time.time(),
    }


@app.get("/api/partners/{partner_name}/files")
async def api_partner_files(partner_name: str):
    """Every file that belongs to THIS partner, and no one else's.

    Three kinds, kept apart because they answer different questions: generated
    event CSVs (the data), the sheets someone uploaded (the source of a
    comparison), and the comparison results themselves.
    """
    _require_partner(partner_name)

    export_rows = store.exports(partner_name, limit=20)
    partner_row = _partner_row(partner_name)

    generated = []
    for row in export_rows:
        stale = _export_is_stale(row, partner_row)
        generated.append({
            "kind": "export", "id": row["id"],
            "name": row["filename"] or f"{partner_name}-events.csv",
            "href": f"/download/export/{row['id']}",
            "status": row["status"],
            "rows": row["rows_written"],
            "total": row["total_rows"],
            "size": row["size_bytes"],
            "scope": row["scope"],
            "created_at": row["finished_at"] or row["requested_at"],
            # Whether the database has changed since the file was written.
            "stale": stale,
            "detail": (
                f"{row['rows_written']:,} events · "
                f"{event_export.describe_scope(row['scope'])}"
                if row["status"] == "done"
                else f"{row['status']}: {row['error'] or 'in progress'}"
            ),
            "available": row["status"] == "done"
            and bool(row["stored_path"]) and os.path.isfile(row["stored_path"]),
        })

    return {
        "partner": partner_name,
        "generated": generated,
        # Always available, and cheap - it is generated from SQLite on request.
        "history": {
            "name": "Count history (CSV)",
            "href": f"/download/partner/{quote(partner_name)}/history.csv",
            "detail": "One row per collection - not the event data",
        },
        "now": time.time(),
    }


# ---------------------------------------------------------------------------
# API - spreadsheet comparison
# ---------------------------------------------------------------------------


def _require_partner(partner_name: str) -> None:
    if partner_name not in store.latest_counts():
        raise HTTPException(status_code=404, detail=f"unknown partner: {partner_name}")


@app.post("/api/partners/{partner_name}/upload")
async def api_upload_sheet(
    partner_name: str,
    request: Request,
    x_filename: str = Header("sheet.csv"),
):
    """Accept a partner spreadsheet as the raw request body.

    The file is sent as the body with the name in a header, rather than as a
    multipart form, because multipart would pull in python-multipart - a seventh
    dependency for a single upload button. `fetch(url, {body: file})` does this
    natively in the browser, so nothing is lost on the client side.
    """
    _require_partner(partner_name)

    declared = int(request.headers.get("content-length") or 0)
    if declared > config.max_upload_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"file is larger than the {config.max_upload_bytes // 1024 // 1024} MB limit",
        )

    data = await request.body()
    if len(data) > config.max_upload_bytes:
        raise HTTPException(status_code=413, detail="file is over the upload limit")

    try:
        parsed = sheets.parse(x_filename, data)
    except sheets.SheetError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if not parsed.matchable:
        raise HTTPException(
            status_code=400,
            detail="no usable column found - the sheet needs a tour/product name, "
                   "an id, or a URL column to compare against the database. "
                   f"Columns seen: {', '.join(parsed.headers[:12])}",
        )

    # Keep the original so the source of a comparison stays retrievable.
    os.makedirs(config.upload_dir, exist_ok=True)
    stored_name = exports.safe_filename(partner_name, str(int(time.time())), x_filename)
    stored_path = os.path.join(config.upload_dir, stored_name)
    with open(stored_path, "wb") as fh:
        fh.write(data)

    rows = parsed.normalised()
    upload_id = store.record_upload(
        partner=partner_name,
        filename=x_filename,
        stored_path=stored_path,
        size_bytes=len(data),
        rows=rows,
        headers=parsed.headers,
        mapping=parsed.mapping,
    )
    return {
        "status": "ok",
        "upload_id": upload_id,
        "rows": len(rows),
        "headers": parsed.headers,
        "mapping": parsed.mapping,
        # Named so the UI can say which column became which field - a wrong
        # guess here is the difference between 0 missing and 85,000.
        "dated": sum(1 for r in rows if r["start_date"]),
    }


@app.post("/api/partners/{partner_name}/compare")
async def api_run_comparison(
    partner_name: str, upload_id: Optional[int] = Query(None)
):
    """Diff the partner's spreadsheet against the database, and store the result."""
    _require_partner(partner_name)

    if not config.queries.get("partner_records"):
        raise HTTPException(
            status_code=501,
            detail="queries.partner_records is not set in config.yaml, so "
                   "record-level comparison is unavailable. See the README.",
        )

    upload = store.upload(upload_id) if upload_id else store.latest_upload(partner_name)
    if not upload:
        raise HTTPException(
            status_code=400,
            detail=f"no spreadsheet has been uploaded for {partner_name} yet",
        )
    if upload["partner"] != partner_name:
        raise HTTPException(status_code=400, detail="that upload belongs to another partner")

    sheet_rows = store.sheet_rows(upload["id"])
    # MySQL is blocking, and this reads rows rather than counting them, so it
    # goes to a thread - a large partner would otherwise stall every other
    # request for the duration.
    result = await asyncio.to_thread(
        compare_mod.compare,
        partner_name,
        sheet_rows,
        config.database,
        config.queries["partner_records"],
        config.max_compare_rows,
    )
    comparison_id = store.record_comparison(partner_name, upload["id"], result)

    return {
        **{k: v for k, v in result.items() if k not in ("missing_rows", "extra_rows")},
        "comparison_id": comparison_id,
        "upload": {k: upload[k] for k in ("id", "filename", "row_count", "uploaded_at", "mapping")},
    }


@app.get("/api/partners/{partner_name}/comparison")
async def api_comparison(partner_name: str):
    """The stored comparison for a partner - counts only, rows load separately."""
    _require_partner(partner_name)
    comparison = store.latest_comparison(partner_name)
    upload = store.latest_upload(partner_name)
    return {
        "comparison": comparison,
        "upload": upload,
        "history": store.comparison_history(partner_name, limit=20),
        "max_compare_rows": config.max_compare_rows,
        "configured": bool(config.queries.get("partner_records")),
    }


@app.get("/api/comparisons/{comparison_id}/rows")
async def api_comparison_rows(
    comparison_id: int,
    side: str = Query("missing", pattern="^(missing|extra)$"),
    q: str = "",
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
):
    """One page of the missing or extra rows. Paged because a large partner has
    tens of thousands and the page must not try to render them all."""
    if not store.comparison(comparison_id):
        raise HTTPException(status_code=404, detail="unknown comparison")
    return {**store.comparison_rows(comparison_id, side, q, offset, limit),
            "side": side, "offset": offset, "limit": limit}


@app.get("/api/partners/{partner_name}/uploads")
async def api_uploads(partner_name: str):
    return {"uploads": store.uploads(partner_name, limit=50)}


@app.delete("/api/uploads/{upload_id}")
async def api_delete_upload(upload_id: int):
    upload = store.upload(upload_id)
    if not upload:
        raise HTTPException(status_code=404, detail="unknown upload")
    # Remove the stored file too. A missing file is not an error here - the row
    # going away is the point, and a leftover path would just fail to download.
    if upload.get("stored_path") and os.path.isfile(upload["stored_path"]):
        try:
            os.remove(upload["stored_path"])
        except OSError:
            pass
    store.delete_upload(upload_id)
    return {"status": "ok", "deleted": upload_id}


# ---------------------------------------------------------------------------
# API - generated event CSVs
#
# The download the team actually wanted: the partner's event rows, not the
# count history. It is a job rather than a response because the biggest
# partners are hundreds of thousands of records - `wcities` is 708,221 rows and
# 120 MB, and takes about five minutes - which no browser will wait for.
# ---------------------------------------------------------------------------


def _drop_superseded(partner_name: str, scope: str, keep: int) -> None:
    """Delete this partner's older exports of the same scope, file and all."""
    for row in store.exports(partner_name, limit=50):
        if row["id"] == keep or row["scope"] != scope:
            continue
        if row["status"] in ("queued", "running"):
            continue
        path = row.get("stored_path")
        if path and os.path.isfile(path):
            try:
                os.remove(path)
            except OSError:
                pass
        store.delete_export(row["id"])


def _export_is_stale(row: Dict[str, Any], partner_row: Dict[str, Any]) -> bool:
    """Has the data moved on since this file was written?

    A generated CSV is a snapshot. If the hourly sweep has since counted this
    partner again, or the partner has inserted a record newer than the file,
    then the file no longer matches the database and the page has to say so -
    otherwise someone downloads yesterday's rows believing they are today's.
    """
    finished = row.get("finished_at")
    if not finished:
        return False
    collected = partner_row.get("collected_at")
    return bool(collected and collected > finished)


async def _run_export(export_id: int, partner_name: str, scope: str) -> None:
    """Build one export, updating its row as it goes. Runs as a background task."""
    stored_name = exports.safe_filename(
        partner_name, scope, time.strftime("%Y%m%d-%H%M%S")
    ) + ".csv"
    path = os.path.join(config.export_dir, stored_name)

    store.update_export(
        export_id, status="running", started_at=time.time(),
        filename=stored_name, stored_path=path,
    )

    # Progress is written straight to SQLite rather than held in memory, so the
    # status endpoint reports it without this task and the request handler
    # having to share anything.
    def progress(rows_done: int) -> None:
        store.update_export(export_id, rows_written=rows_done)

    try:
        result = await asyncio.to_thread(
            event_export.build,
            partner_name,
            scope,
            config.database,
            path,
            config.event_columns,
            config.queries.get("partner_events") or event_export.DEFAULT_EVENT_QUERY,
            config.queries.get("venue_details") or event_export.DEFAULT_VENUE_QUERY,
            config.max_export_rows or None,
            progress,
        )
        store.update_export(
            export_id, status="done", rows_written=result["rows"],
            size_bytes=result["size"], finished_at=time.time(), error=None,
        )
        # The file this one replaces is deleted immediately rather than left to
        # the nightly prune. A list of five same-named CSVs from five different
        # afternoons is how someone downloads last week's data by accident -
        # only the current one should ever be on offer.
        _drop_superseded(partner_name, scope, keep=export_id)
    except Exception as exc:
        # The half-written file is removed: a partial CSV that downloads
        # cleanly is worse than none, because nothing about it says it is short.
        if os.path.isfile(path):
            try:
                os.remove(path)
            except OSError:
                pass
        store.update_export(
            export_id, status="failed", finished_at=time.time(),
            error=f"{type(exc).__name__}: {exc}",
        )


@app.post("/api/partners/{partner_name}/export")
async def api_start_export(
    partner_name: str,
    scope: str = Query("all", pattern="^(all|live|unpublished)$"),
):
    """Start generating this partner's event CSV, or join the run in progress."""
    _require_partner(partner_name)

    if not config.queries.get("partner_events"):
        raise HTTPException(
            status_code=501,
            detail="queries.partner_events is not set in config.yaml, so the "
                   "event CSV cannot be generated. See the README.",
        )

    # Pressing the button twice must not start a second scan of the same
    # partner - it joins the first, which is also what a second person opening
    # the page should get.
    running = store.active_export(partner_name)
    if running:
        return {"export": running, "joined": True}

    row = _partner_row(partner_name)
    expected = row.get("feed_total") if scope == "all" else (
        row.get("db_future") if scope == "live" else row.get("db_unpublished")
    )

    export_id = store.create_export(partner_name, scope, total_rows=expected)
    asyncio.create_task(_run_export(export_id, partner_name, scope))
    return {"export": store.export(export_id), "joined": False}


@app.get("/api/partners/{partner_name}/export")
async def api_partner_export(partner_name: str):
    """The newest export for this partner, plus its recent history."""
    _require_partner(partner_name)
    latest = store.latest_export(partner_name)
    return {
        "export": latest,
        "history": store.exports(partner_name, limit=10),
        "scopes": [
            {"key": key, "detail": detail}
            for key, (_, detail) in event_export.SCOPES.items()
        ],
        "configured": bool(config.queries.get("partner_events")),
        "now": time.time(),
    }


@app.get("/api/exports/{export_id}")
async def api_export_status(export_id: int):
    """One export's status. Polled by the page while it generates."""
    row = store.export(export_id)
    if not row:
        raise HTTPException(status_code=404, detail="unknown export")
    return {"export": row, "now": time.time()}


@app.delete("/api/exports/{export_id}")
async def api_delete_export(export_id: int):
    row = store.export(export_id)
    if not row:
        raise HTTPException(status_code=404, detail="unknown export")
    if row["status"] == "running":
        raise HTTPException(
            status_code=409,
            detail="this export is still generating - wait for it to finish",
        )
    if row.get("stored_path") and os.path.isfile(row["stored_path"]):
        try:
            os.remove(row["stored_path"])
        except OSError:
            pass
    store.delete_export(export_id)
    return {"status": "ok", "deleted": export_id}


@app.get("/download/export/{export_id}")
async def download_export(export_id: int):
    row = store.export(export_id)
    if not row:
        raise HTTPException(status_code=404, detail="unknown export")
    if row["status"] != "done":
        raise HTTPException(
            status_code=409,
            detail=f"this export is {row['status']}"
                   + (f": {row['error']}" if row.get("error") else
                      " - it is not ready to download yet"),
        )
    path = row.get("stored_path")
    if not path or not os.path.isfile(path):
        raise HTTPException(
            status_code=410,
            detail="the generated file is no longer on disk - generate it again",
        )
    return FileResponse(path, media_type="text/csv", filename=row["filename"])


# ---------------------------------------------------------------------------
# API - issues
# ---------------------------------------------------------------------------


@app.get("/api/issues")
async def api_issues(
    severity: str = "", scope: str = "", kind: str = "", q: str = "",
):
    state = full_state()
    found = state["issues"]

    if severity:
        found = [i for i in found if i["severity"] == severity]
    if scope:
        found = [i for i in found if i["scope"] == scope]
    if kind:
        found = [i for i in found if i["kind"] == kind]
    if q.strip():
        needle = q.strip().lower()
        found = [
            i for i in found
            if needle in i["subject"].lower() or needle in i["title"].lower()
            or needle in i["detail"].lower()
        ]

    return {
        "issues": found,
        # The summary always describes everything, not the filtered subset, so
        # the tiles do not move when a filter is applied.
        "summary": issues_mod.summarise(state["issues"]),
        "now": time.time(),
    }


# ---------------------------------------------------------------------------
# API - logs
# ---------------------------------------------------------------------------


def _log_files() -> List[Dict[str, Any]]:
    return applog.available(BASE_DIR, extra=[config.log_file])


def _resolve_log(name: str) -> str:
    """Map a requested log name to a path we are willing to read.

    Matched against the known list rather than joined onto a directory: a name
    is user input, and `../../etc/passwd` must not resolve to anything.
    """
    for entry in _log_files():
        if entry["name"] == name:
            return entry["path"]
    raise HTTPException(status_code=404, detail=f"unknown log file: {name}")


@app.get("/api/logs/files")
async def api_log_files():
    files = _log_files()
    for entry in files:
        entry["modified_iso"] = _iso(entry["modified"])
    return {"files": files}


@app.get("/api/logs")
async def api_logs(
    file: str = "",
    q: str = "",
    level: str = "",
    status_class: str = "",
    since: Optional[float] = None,
    until: Optional[float] = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=1000),
):
    files = _log_files()
    if not files:
        return {"lines": [], "total": 0, "file": None, "files": [],
                "summary": {}, "error": "no log files found"}

    name = file or os.path.basename(config.log_file) or files[0]["name"]
    path = _resolve_log(name)

    result = applog.read(
        path, query=q, level=level, since=since, until=until,
        status_class=status_class, offset=offset, limit=limit,
    )
    return {
        **result,
        "file": name,
        "files": [f["name"] for f in files],
        "summary": applog.summarise(path),
        "offset": offset,
        "limit": limit,
    }


# ---------------------------------------------------------------------------
# API - website health
# ---------------------------------------------------------------------------


@app.get("/api/sites")
async def api_sites():
    return {
        "sites": build_site_rows(),
        # False means nothing here ever requests these URLs; the rows are the
        # last stored result and Processes is the live signal.
        "checks_enabled": config.health_checks_enabled,
        "jobs": scheduler.status(),
        # So a misconfigured mail setup is visible on the page itself rather
        # than only discovered the day an outage goes unreported.
        "alerts": scheduler.alerter.status(),
        "now": time.time(),
    }


@app.get("/api/sites/history")
async def api_site_history(url: str, limit: int = Query(60, ge=1, le=500)):
    return {"history": store.check_history(url, limit)}


@app.post("/api/sites/refresh")
async def api_sites_refresh(url: Optional[str] = None):
    return await scheduler.run_health(url=url)


# ---------------------------------------------------------------------------
# API - down alerts
# ---------------------------------------------------------------------------


@app.get("/api/alerts")
async def api_alerts(limit: int = Query(50, ge=1, le=500)):
    """Alert configuration plus the log of what was actually sent."""
    return {"status": scheduler.alerter.status(), "log": store.recent_alerts(limit)}


@app.post("/api/alerts/test")
async def api_alerts_test():
    """Send a test mail to every configured recipient.

    The honest way to find out whether alerts work, without waiting for a real
    outage:  curl -X POST http://localhost:5603/api/alerts/test
    """
    return await scheduler.alerter.send_test()


# ---------------------------------------------------------------------------
# API - PM2 processes
# ---------------------------------------------------------------------------


@app.post("/api/pm2/report")
async def api_pm2_report(payload: Dict[str, Any], x_agent_secret: str = Header(None)):
    """Heartbeat endpoint for agent.py running on each target server."""
    if x_agent_secret != config.agent_secret:
        raise HTTPException(status_code=401, detail="Invalid token")
    server_id = payload.get("server_id", "unknown-server")
    PM2_STORE[server_id] = {
        "server_id": server_id,
        "processes": payload.get("processes", []),
        "last_updated": time.time(),
    }
    return {"status": "ok"}


class CronReport(BaseModel):
    """A server's own crontab, pushed by agent.py.

    The server reads `crontab -l` and stats its own log files, so the dashboard
    needs no SSH access, no credentials and no inbound login anywhere. Parsing
    stays here rather than in the agent: cron_parse is the tested code, and the
    agent must remain a dependency-free file that runs on old Pythons.
    """
    server_id: str
    # How the rows are keyed - the server's IP, to match the partner sheet.
    server: Optional[str] = None
    hostname: Optional[str] = None
    crontab: str = ""
    # path -> {"mtime": float, "size": int}, stat-ed on the server itself.
    logs: Dict[str, Dict[str, Any]] = Field(default_factory=dict)


@app.post("/api/cron/report")
async def api_cron_report(report: CronReport, x_agent_secret: str = Header(None)):
    if x_agent_secret != config.agent_secret:
        raise HTTPException(status_code=401, detail="Invalid token")

    rows = cron_parse.parse_crontab(report.crontab, known_partner_names())
    for row in rows:
        stat = report.logs.get(row["log_file"]) if row["log_file"] else None
        if stat:
            row["log_mtime"] = stat.get("mtime")
            row["log_size"] = stat.get("size")

    server = report.server or report.server_id
    # Wholesale replace, exactly as the SSH collector did: a job deleted on the
    # server has to disappear here too, not linger as a phantom.
    store.replace_cron_jobs(server, report.hostname or report.server_id, rows)
    _CRON_CACHE["at"] = 0.0          # new data - drop the cross-reference cache
    return {"status": "ok", "server": server, "jobs": len(rows)}


@app.get("/api/pm2/status")
async def api_pm2_status():
    return {
        "servers": build_server_rows(),
        "stale_after_seconds": config.pm2_stale_seconds,
        "now": time.time(),
    }


# ---------------------------------------------------------------------------
# API - jobs / diagnostics
# ---------------------------------------------------------------------------


@app.get("/api/jobs")
async def api_jobs():
    return {"jobs": scheduler.status(), "now": time.time()}


# Cached so the banner doesn't open a MySQL connection on every poll.
_DB_IDENTITY: Dict[str, Any] = {}


@app.get("/api/jobs/partners")
async def api_partner_jobs():
    """Ingest-job freshness per partner, worst first."""
    state = full_state()
    rows = state["partners"]

    for row in rows:
        row["job_rank"] = jobs_mod.STATE_RANK.get(row.get("job_state"), 9)
    # A stalled partner whose cron is also missing is the most actionable thing
    # on the page, so it sorts above other stalled rows.
    rows.sort(key=lambda r: (
        r["job_rank"],
        0 if r.get("cron_status") == "missing" else 1,
        -(r.get("db_future") or 0),
    ))

    counts: Dict[str, int] = {}
    for row in rows:
        counts[row.get("job_state", "unknown")] = counts.get(row.get("job_state", "unknown"), 0) + 1
    return {
        "partners": rows,
        "summary": {
            "total": len(rows),
            "stalled": counts.get("stalled", 0),
            "late": counts.get("late", 0),
            "ok": counts.get("ok", 0),
            "unknown": counts.get("unknown", 0),
            "never": counts.get("never", 0),
            "dormant": counts.get("dormant", 0),
            "retired": counts.get("retired", 0),
            # The headline: stopped inserting AND nothing scheduled to fix it.
            "stalled_no_cron": sum(
                1 for r in rows
                if r.get("job_state") == "stalled" and r.get("cron_status") == "missing"
            ),
            "cron_collected": len(cron_index()[1]),
        },
        "jobs": scheduler.status(),
        "now": time.time(),
    }


@app.get("/api/cron")
async def api_cron():
    """Every crontab line collected from the servers, newest write first."""
    rows = store.cron_jobs()
    now = time.time()
    counted = set(store.latest_counts().keys())
    for r in rows:
        r["log_age_days"] = (
            round((now - r["log_mtime"]) / 86400.0, 1) if r["log_mtime"] else None
        )
        # Rows collected before categories existed have none stored. Deriving it
        # here rather than back-filling the table keeps the answer current with
        # the rules in cron_parse, and costs nothing at this scale.
        if not r.get("category"):
            r["category"] = cron_parse.categorise(
                r.get("script"), r.get("command") or "", r.get("name") or ""
            )
        r["category_label"] = cron_parse.CATEGORY_LABELS.get(r["category"], "Other")
        # Whether that partner has a page to link to.
        #
        # Some partners have cron jobs on disk and no rows in MySQL at all -
        # sportsradar has 18 scheduled jobs and nothing in the database, and
        # fandango, ticketsnow, reservix and bemyguest are the same shape.
        # Naming them is useful (a job running for a partner we hold nothing
        # for is worth seeing), but /partners/<name> 404s for them, so the UI
        # needs to know not to link.
        r["partner_known"] = bool(r["partner"]) and r["partner"] in counted
    servers = store.cron_servers()
    if config.cron_source == "agent":
        # Nothing is scheduled on this side - the servers push. Report when the
        # last one did, so a silent agent is visible rather than looking like
        # data that simply never changes.
        reported = [s["collected_at"] for s in servers if s.get("collected_at")]
        job = {
            "name": "crons",
            "schedule": "push",
            "interval_seconds": 0,
            "last_run": max(reported) if reported else None,
            "next_run": None,
            "running": False,
            "progress": "",
            "last_error": None,
            "runs": len(servers),
            "runs_24h": sum(1 for t in reported if t >= now - 86400),
            "servers_reporting": len(servers),
        }
    else:
        job = scheduler.status().get("crons")
    by_category = {key: 0 for key, _, _ in cron_parse.CATEGORIES}
    for r in rows:
        by_category[r["category"]] = by_category.get(r["category"], 0) + 1

    return {
        "jobs": rows,
        "servers": servers,
        "job": job,
        "source": config.cron_source,
        # Declared order, and every category present even at zero, so the
        # filter list does not reshuffle as jobs come and go.
        "categories": [
            {"key": key, "label": label, "detail": detail,
             "count": by_category.get(key, 0)}
            for key, label, detail in cron_parse.CATEGORIES
        ],
        "summary": {
            "total": len(rows),
            "servers": len(servers),
            "disabled": sum(1 for r in rows if r["disabled"]),
            "with_partner": sum(1 for r in rows if r["partner"]),
            "with_log": sum(1 for r in rows if r["log_mtime"]),
            "by_category": by_category,
        },
        "now": now,
    }


# ---------------------------------------------------------------------------
# A cron job's own output file
#
# The Processes table records where each job redirects its output and when that
# file was last written, which answers "did it run" but never "what did it
# say". These two fetch the file itself from the server it lives on.
#
# The path is always taken from the stored crontab row, never from the request.
# A path is user input, and nothing here should be able to fetch /etc/shadow
# because someone typed it into a URL.
# ---------------------------------------------------------------------------

# Enough to hold a big run log's tail, small enough to stay a quick request.
CRON_OUTPUT_CAP = 25 * 1024 * 1024


def _cron_output_job(job_id: int) -> Dict[str, Any]:
    job = store.cron_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="unknown cron job")
    if not job.get("log_file"):
        raise HTTPException(
            status_code=400,
            detail="this crontab line has no output redirect, so there is no "
                   "file to fetch - nothing about it can be shown.",
        )
    return job


@app.get("/api/cron/{job_id}/output")
async def api_cron_output(job_id: int, lines: int = Query(200, ge=1, le=5000)):
    """The last few lines of a job's output, for reading in the browser."""
    job = _cron_output_job(job_id)
    ok, result = await asyncio.to_thread(
        cron_collect.fetch_output, job["server"], job["log_file"],
        # A preview only needs the tail; 2 MB is far more than `lines` of text.
        2 * 1024 * 1024, False,
    )
    if not ok:
        raise HTTPException(status_code=502, detail=result)

    text = result.decode("utf-8", errors="replace")
    tail = text.splitlines()[-lines:]
    return {
        "job": {k: job.get(k) for k in ("id", "name", "server", "log_file",
                                        "log_size", "log_mtime", "partner")},
        "lines": tail,
        "truncated": len(text.splitlines()) > len(tail),
        "now": time.time(),
    }


@app.get("/download/cron/{job_id}/output")
async def download_cron_output(job_id: int, whole: bool = False):
    """Download a cron job's output file.

    Defaults to the recent tail. `?whole=true` asks for the entire file and is
    refused above the cap rather than truncated - several of these are hundreds
    of megabytes, and a CSV cut off mid-row looks like a complete file that is
    quietly missing records.
    """
    job = _cron_output_job(job_id)
    ok, result = await asyncio.to_thread(
        cron_collect.fetch_output, job["server"], job["log_file"],
        CRON_OUTPUT_CAP, whole,
    )
    if not ok:
        raise HTTPException(status_code=502, detail=result)

    base = os.path.basename(job["log_file"]) or "output"
    name = exports.safe_filename(job["server"], base)
    if not whole:
        name = exports.safe_filename(job["server"], "recent", base)
    media = "text/csv; charset=utf-8" if base.lower().endswith(".csv") \
        else "text/plain; charset=utf-8"
    return Response(
        content=result,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


# --- big files: fetched in the background, downloaded when ready -----------


async def _run_fetch(fetch_id: int, job: Dict[str, Any]) -> None:
    """Pull one whole output file off its server. Runs as a background task."""
    base = os.path.basename(job["log_file"]) or "output"
    stored_name = exports.safe_filename(
        job["server"], base, time.strftime("%Y%m%d-%H%M%S")
    )
    path = os.path.join(config.fetch_dir, stored_name)

    ok, size = await asyncio.to_thread(
        cron_collect.remote_size, job["server"], job["log_file"]
    )
    store.update_fetch(
        fetch_id, status="running", started_at=time.time(),
        filename=stored_name, stored_path=path,
        total_bytes=size if ok else None,
    )

    def progress(done: int) -> None:
        store.update_fetch(fetch_id, bytes_fetched=done)

    ok, result = await asyncio.to_thread(
        cron_collect.stream_to_disk, job["server"], job["log_file"], path,
        progress, config.max_fetch_bytes,
    )
    if ok:
        store.update_fetch(
            fetch_id, status="done", bytes_fetched=result,
            finished_at=time.time(), error=None,
        )
        # Only the newest copy of a given job's output is kept, for the same
        # reason as the event CSVs: a pile of same-named files from different
        # afternoons is how someone opens the wrong one.
        _drop_old_fetches(job["id"], keep=fetch_id)
    else:
        if os.path.isfile(path):
            try:
                os.remove(path)     # a half-copied log is worse than none
            except OSError:
                pass
        store.update_fetch(
            fetch_id, status="failed", finished_at=time.time(), error=str(result),
        )


def _drop_old_fetches(job_id: int, keep: int) -> None:
    for row in store.fetches(limit=200):
        if row["job_id"] != job_id or row["id"] == keep:
            continue
        if row["status"] in ("queued", "running"):
            continue
        if row.get("stored_path") and os.path.isfile(row["stored_path"]):
            try:
                os.remove(row["stored_path"])
            except OSError:
                pass
        store.delete_fetch(row["id"])


@app.post("/api/cron/{job_id}/fetch")
async def api_start_fetch(job_id: int):
    """Start pulling this job's whole output file, however large it is."""
    job = _cron_output_job(job_id)

    running = store.active_fetch(job_id)
    if running:
        return {"fetch": running, "joined": True}

    fetch_id = store.create_fetch(
        job_id, job["server"], job["log_file"], total_bytes=job.get("log_size")
    )
    asyncio.create_task(_run_fetch(fetch_id, job))
    return {"fetch": store.fetch_row(fetch_id), "joined": False}


@app.get("/api/cron/{job_id}/fetch")
async def api_fetch_status(job_id: int):
    """The newest transfer for this job, polled while it runs."""
    row = store.latest_fetch(job_id)
    if row and row["status"] == "done":
        row["available"] = bool(row.get("stored_path")
                                and os.path.isfile(row["stored_path"]))
    return {"fetch": row, "now": time.time()}


@app.get("/download/cron-fetch/{fetch_id}")
async def download_cron_fetch(fetch_id: int):
    row = store.fetch_row(fetch_id)
    if not row:
        raise HTTPException(status_code=404, detail="unknown transfer")
    if row["status"] != "done":
        raise HTTPException(
            status_code=409,
            detail=f"this transfer is {row['status']}"
                   + (f": {row['error']}" if row.get("error") else
                      " - it is not ready yet"),
        )
    path = row.get("stored_path")
    if not path or not os.path.isfile(path):
        raise HTTPException(
            status_code=410,
            detail="the fetched copy is no longer on disk - fetch it again",
        )
    base = os.path.basename(row["remote_path"]) or "output"
    media = "text/csv; charset=utf-8" if base.lower().endswith(".csv") \
        else "text/plain; charset=utf-8"
    return FileResponse(path, media_type=media, filename=base)


@app.get("/api/db/identity")
async def api_db_identity():
    """Which MySQL server is behind these numbers.

    Surfaced in the UI on purpose: the laptop copy and the master both answer on
    127.0.0.1:3306 with an `admin` schema, and they hold different data. Showing
    the hostname stops anyone reading the wrong numbers without realising.
    """
    if not _DB_IDENTITY:
        _DB_IDENTITY.update(await asyncio.to_thread(mysql.ping, config.database))
    return _DB_IDENTITY


@app.get("/api/db/ping")
async def api_db_ping():
    """Connectivity check against the partner MySQL - runs SELECT VERSION()."""
    return mysql.ping(config.database)


@app.get("/api/settings")
async def api_settings():
    """What this instance is configured to do. Read-only on purpose.

    Everything here comes from config.yaml and .env, which are the record. A
    settings page that could write them would put the running configuration and
    the file it was loaded from permanently out of step.
    """
    return {
        "app": {
            "counts_hourly": config.counts_hourly,
            "counts_interval_seconds": config.counts_interval,
            "health_interval_seconds": config.health_interval,
            "health_checks_enabled": config.health_checks_enabled,
            "cron_source": config.cron_source,
            "pm2_stale_seconds": config.pm2_stale_seconds,
            "history_keep_days": config.history_keep_days,
            "max_compare_rows": config.max_compare_rows,
            "max_upload_mb": round(config.max_upload_bytes / 1024 / 1024),
            "scheduler_enabled": SCHEDULER_ENABLED,
        },
        "database": {
            "host": config.database.get("host"),
            "port": config.database.get("port"),
            "user": config.database.get("user"),
            "database": config.database.get("database"),
            # Whether one is set, never the value.
            "password_set": config.has_db_password,
        },
        "auth": {"enabled": config.auth_enabled, "user": config.auth_user},
        "alerts": scheduler.alerter.status(),
        "partners": {
            "discovered": len(store.latest_counts()),
            "excluded": sorted(config.excluded),
            "min_live_events": config.min_live_events,
            "with_meta": len(config.partner_meta),
        },
        "websites": len(config.websites),
        "paths": {
            "config": os.environ.get("OPS_CONFIG", os.path.join(BASE_DIR, "config.yaml")),
            "database": config.db_path,
            "uploads": config.upload_dir,
            "log": config.log_file,
        },
        "queries": {
            # The SQL itself, so what produced a number is inspectable without
            # opening config.yaml on the server.
            key: config.queries.get(key)
            for key in ("all_partners", "feed_total", "db_future", "db_past", "partner_records")
        },
        "jobs": scheduler.status(),
        "now": time.time(),
    }


# ---------------------------------------------------------------------------
# Downloads
# ---------------------------------------------------------------------------


def csv_response(rows: Iterable[Dict[str, Any]], columns: Sequence[exports.Column],
                 filename: str) -> StreamingResponse:
    return StreamingResponse(
        exports.to_csv(rows, columns),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/downloads")
async def api_downloads():
    """The catalogue the Downloads page lists.

    Generated exports are always available; files are listed only when they
    exist, so nothing offers a download that would 404.
    """
    log_files = _log_files()
    uploads = store.uploads(limit=200)
    comparisons = store.latest_comparisons()

    generated = [
        {"kind": "csv", "name": "All partners", "href": "/download/partners.csv",
         "detail": "Every partner with counts, job state, cron status and issue count"},
        {"kind": "csv", "name": "All issues", "href": "/download/issues.csv",
         "detail": "Every open issue across partners, sites and processes"},
        {"kind": "csv", "name": "Website health", "href": "/download/sites.csv",
         "detail": "Latest check, 24h uptime and latency per site"},
        {"kind": "csv", "name": "Cron inventory", "href": "/download/cron.csv",
         "detail": "Every crontab line collected from every reporting server"},
    ]

    comparison_files = []
    for partner, comparison in sorted(comparisons.items()):
        for side, label in (("missing", "Missing from database"), ("extra", "Extra in database")):
            count = comparison.get(side) or 0
            if count:
                comparison_files.append({
                    "kind": "comparison", "name": f"{partner} - {label.lower()}",
                    "href": f"/download/comparison/{comparison['id']}/{side}.csv",
                    "detail": f"{count:,} rows, compared {_iso(comparison['computed_at'])}",
                    "partner": partner, "count": count,
                })

    # The generated per-partner event CSVs. Listed only when the file is still
    # on disk, so nothing here offers a download that would 404.
    event_files = [{
        "kind": "export", "name": f"{row['partner']} - events ({row['scope']})",
        "href": f"/download/export/{row['id']}",
        "detail": f"{row['rows_written']:,} events · "
                  f"{(row['size_bytes'] or 0) / 1024 / 1024:.1f} MB · "
                  f"generated {_iso(row['finished_at'])}",
        "partner": row["partner"],
    } for row in store.exports(limit=100)
        if row["status"] == "done" and row.get("stored_path")
        and os.path.isfile(row["stored_path"])]

    return {
        "generated": generated,
        "events": event_files,
        "logs": [{
            "kind": "log", "name": entry["name"],
            "href": f"/download/log/{entry['name']}",
            "detail": f"{entry['size'] / 1024:.0f} KB, modified {_iso(entry['modified'])}",
            "size": entry["size"], "modified": entry["modified"],
        } for entry in log_files],
        "comparisons": comparison_files,
        "uploads": [{
            "kind": "upload", "name": upload["filename"],
            "href": f"/download/upload/{upload['id']}",
            "detail": f"{upload['partner']} · {upload['row_count'] or 0:,} rows · "
                      f"uploaded {_iso(upload['uploaded_at'])}",
            "partner": upload["partner"], "id": upload["id"],
            "uploaded_at": upload["uploaded_at"],
            "available": bool(upload.get("stored_path")
                              and os.path.isfile(upload["stored_path"])),
        } for upload in uploads],
        "now": time.time(),
    }


@app.get("/download/partners.csv")
async def download_partners():
    rows = full_state()["partners"]
    return csv_response(rows, exports.PARTNER_COLUMNS, "partners.csv")


@app.get("/download/issues.csv")
async def download_issues():
    found = full_state()["issues"]
    for issue in found:
        issue["since_iso"] = _iso(issue.get("since"))
        issue["last_run_iso"] = _iso(issue.get("last_run"))
    return csv_response(found, exports.ISSUE_COLUMNS, "issues.csv")


@app.get("/download/sites.csv")
async def download_sites():
    return csv_response(build_site_rows(), exports.SITE_COLUMNS, "website-health.csv")


@app.get("/download/cron.csv")
async def download_cron():
    rows = store.cron_jobs()
    now = time.time()
    for row in rows:
        row["log_age_days"] = (
            round((now - row["log_mtime"]) / 86400.0, 1) if row["log_mtime"] else None
        )
        row["disabled"] = bool(row["disabled"])
        if not row.get("category"):
            row["category"] = cron_parse.categorise(
                row.get("script"), row.get("command") or "", row.get("name") or ""
            )
        row["category_label"] = cron_parse.CATEGORY_LABELS.get(row["category"], "Other")
    return csv_response(rows, exports.CRON_COLUMNS, "cron-inventory.csv")


@app.get("/download/partner/{partner_name}/history.csv")
async def download_partner_history(partner_name: str):
    _require_partner(partner_name)
    rows = store.counts_history(partner_name, limit=500)
    for row in rows:
        row["collected_at_iso"] = _iso(row["collected_at"])
        row["ok"] = bool(row["ok"])
        row["db_unpublished"] = (
            max(row["feed_total"] - row["db_future"] - row["db_past"], 0)
            if None not in (row["feed_total"], row["db_future"], row["db_past"])
            else None
        )
    return csv_response(
        rows, exports.HISTORY_COLUMNS,
        exports.safe_filename(partner_name, "history") + ".csv",
    )


@app.get("/download/comparison/{comparison_id}/{side}.csv")
async def download_comparison(comparison_id: int, side: str):
    if side not in ("missing", "extra"):
        raise HTTPException(status_code=400, detail="side must be missing or extra")
    comparison = store.comparison(comparison_id)
    if not comparison:
        raise HTTPException(status_code=404, detail="unknown comparison")
    rows = store.all_comparison_rows(comparison_id, side)
    return csv_response(
        rows, exports.COMPARISON_COLUMNS,
        exports.safe_filename(comparison["partner"], side) + ".csv",
    )


@app.get("/download/log/{name}")
async def download_log(name: str):
    path = _resolve_log(name)
    return FileResponse(path, media_type="text/plain", filename=name)


@app.get("/download/logs.csv")
async def download_log_csv(
    file: str = "", q: str = "", level: str = "", status_class: str = "",
):
    """The filtered log view as a CSV - what is on screen, not the whole file."""
    files = _log_files()
    if not files:
        raise HTTPException(status_code=404, detail="no log files found")
    name = file or os.path.basename(config.log_file) or files[0]["name"]
    result = applog.read(
        _resolve_log(name), query=q, level=level,
        status_class=status_class, offset=0, limit=100_000,
    )
    return csv_response(
        result["lines"], exports.LOG_COLUMNS,
        exports.safe_filename(name.replace(".log", ""), "filtered") + ".csv",
    )


@app.get("/download/upload/{upload_id}")
async def download_upload(upload_id: int):
    """The original spreadsheet back, exactly as it was uploaded."""
    upload = store.upload(upload_id)
    if not upload:
        raise HTTPException(status_code=404, detail="unknown upload")
    path = upload.get("stored_path")
    if not path or not os.path.isfile(path):
        raise HTTPException(
            status_code=410,
            detail="the stored copy of this file is no longer on disk",
        )
    return FileResponse(path, filename=upload["filename"])


@app.get("/download/partner-status.csv")
async def download_partner_status():
    """The Monday partner status sheet, if it is present beside the code."""
    path = os.path.join(BASE_DIR, "partner-status.csv")
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="partner-status.csv is not present")
    return FileResponse(path, media_type="text/csv", filename="partner-status.csv")


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


@app.get("/api/reports/summary")
async def api_report_summary():
    """The management view: totals, health split, and the worst offenders.

    Everything here is derived from the same rows the operational pages use, so
    a report can never disagree with the dashboard it summarises.
    """
    state = full_state()
    partners = state["partners"]
    summary = build_summary(partners)
    issue_summary = issues_mod.summarise(state["issues"])

    by_status: Dict[str, int] = {}
    for row in partners:
        by_status[row["status"]] = by_status.get(row["status"], 0) + 1

    def top(rows: List[Dict[str, Any]], key: str, limit: int = 10) -> List[Dict[str, Any]]:
        ranked = [r for r in rows if (r.get(key) or 0) > 0]
        ranked.sort(key=lambda r: -(r.get(key) or 0))
        return [{"name": r["name"], "value": r.get(key), "status": r["status"]}
                for r in ranked[:limit]]

    compared = [p for p in partners if p.get("comparison")]
    for row in compared:
        row["_missing"] = row["comparison"].get("missing") or 0
        row["_extra"] = row["comparison"].get("extra") or 0

    stalled = [p for p in partners if p.get("job_state") in ("stalled", "never")]
    stalled.sort(key=lambda r: -(r.get("db_future") or 0))

    return {
        "generated_at": time.time(),
        "totals": summary,
        "issues": issue_summary,
        "by_status": by_status,
        "health": {
            "healthy": by_status.get("success", 0),
            "warning": by_status.get("warning", 0),
            "failed": by_status.get("failed", 0),
            "total": len(partners),
            "healthy_pct": _pct(by_status.get("success", 0), len(partners)),
        },
        "biggest_partners": top(partners, "db_future"),
        "most_unpublished": top(partners, "db_unpublished"),
        "most_missing": top(compared, "_missing"),
        "most_extra": top(compared, "_extra"),
        "stalled": [{
            "name": r["name"], "live": r.get("db_future"),
            "days": r.get("job_days_since"), "frequency": r.get("frequency"),
            "cron_status": r.get("cron_status"), "server": r.get("server"),
        } for r in stalled[:15]],
        "comparison_coverage": {
            "compared": len(compared),
            "total": len(partners),
            "pct": _pct(len(compared), len(partners)),
        },
        "sites": {
            "total": summary["sites_total"],
            "down": summary["sites_down"],
        },
        "processes": {
            "servers": summary["servers_total"],
            "stale": summary["servers_stale"],
            "total": summary["processes_total"],
            "errored": summary["processes_errored"],
        },
    }


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


if __name__ == "__main__":
    import uvicorn

    # 5603 is the port this dashboard is allocated - 8000 is not available to us.
    # Overridable so a second instance can run alongside the first, and so this
    # entry point agrees with run-with-tunnel.sh instead of hardcoding its own.
    uvicorn.run(
        app,
        host=os.environ.get("APP_HOST", "0.0.0.0"),
        port=int(os.environ.get("APP_PORT", "5603")),
    )
