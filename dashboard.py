"""Ops Dashboard - partner event counts, website health and PM2 processes.

Run with:  ./venv/bin/python dashboard.py
"""
import asyncio
import os
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

import cron_parse
import jobs as jobs_mod
import mysql
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    if SCHEDULER_ENABLED:
        scheduler.start()
    yield
    await scheduler.stop()


app = FastAPI(title="Ops Dashboard", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def _pct(part: Optional[int], whole: Optional[int]) -> Optional[float]:
    if not whole or part is None:
        return None
    return round(100.0 * part / whole, 2)


def known_partners() -> List[str]:
    """Partners we have counts for - discovered from the database, not configured."""
    return sorted(store.latest_counts().keys(), key=str.lower)


def build_partner_rows() -> List[Dict[str, Any]]:
    """Discovered partners joined with their latest counts, plus deltas."""
    latest = store.latest_counts()
    previous = store.previous_counts()
    feeds = store.latest_feed_counts()

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
                "duration_ms": cur["duration_ms"] if cur else None,
            }
        )
    return rows


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


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


@app.get("/")
async def page_partners(request: Request):
    return templates.TemplateResponse(request, "partners.html", {"active": "partners"})


@app.get("/partners/{partner_name}")
async def page_partner_detail(request: Request, partner_name: str):
    if partner_name not in store.latest_counts():
        raise HTTPException(status_code=404, detail=f"unknown partner: {partner_name}")
    partner = {"name": partner_name, **config.meta_for(partner_name)}
    return templates.TemplateResponse(
        request, "partner_detail.html", {"active": "partners", "partner": partner}
    )


@app.get("/sites")
async def page_sites(request: Request):
    return templates.TemplateResponse(request, "sites.html", {"active": "sites"})


@app.get("/jobs")
async def page_jobs(request: Request):
    return templates.TemplateResponse(request, "jobs.html", {"active": "jobs"})


@app.get("/cron")
async def page_cron(request: Request):
    return templates.TemplateResponse(request, "cron.html", {"active": "cron"})


@app.get("/processes")
async def page_processes(request: Request):
    return templates.TemplateResponse(request, "processes.html", {"active": "processes"})


# ---------------------------------------------------------------------------
# API - partners
# ---------------------------------------------------------------------------


@app.get("/api/partners")
async def api_partners():
    rows = build_partner_rows()
    return {
        "partners": rows,
        "summary": build_summary(rows),
        "jobs": scheduler.status(),
        "now": time.time(),
    }


@app.get("/api/partners/{partner_name}")
async def api_partner(partner_name: str):
    for row in build_partner_rows():
        if row["name"] == partner_name:
            return {"partner": row, "jobs": scheduler.status(), "now": time.time()}
    raise HTTPException(status_code=404, detail=f"unknown partner: {partner_name}")


@app.get("/api/partners/{partner_name}/history")
async def api_partner_history(partner_name: str, limit: int = Query(60, ge=1, le=500)):
    if partner_name not in store.latest_counts():
        raise HTTPException(status_code=404, detail=f"unknown partner: {partner_name}")
    return {"history": store.counts_history(partner_name, limit)}


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
# API - website health
# ---------------------------------------------------------------------------


@app.get("/api/sites")
async def api_sites():
    return {
        "sites": build_site_rows(),
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

    rows = cron_parse.parse_crontab(report.crontab, set(config.partner_meta.keys()))
    for row in rows:
        stat = report.logs.get(row["log_file"]) if row["log_file"] else None
        if stat:
            row["log_mtime"] = stat.get("mtime")
            row["log_size"] = stat.get("size")

    server = report.server or report.server_id
    # Wholesale replace, exactly as the SSH collector did: a job deleted on the
    # server has to disappear here too, not linger as a phantom.
    store.replace_cron_jobs(server, report.hostname or report.server_id, rows)
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
    """Ingest-job freshness per partner, worst first.

    Cross-references the collected crontabs, because "stopped inserting" plus
    "has no cron entry" together mean the job was removed - which neither fact
    establishes on its own.
    """
    rows = build_partner_rows()

    cron_rows = store.cron_jobs()
    collected_servers = {s["server"] for s in store.cron_servers()}
    by_partner: Dict[str, List[Dict[str, Any]]] = {}
    for cr in cron_rows:
        if cr["partner"]:
            by_partner.setdefault(cr["partner"].lower(), []).append(cr)

    for r in rows:
        entries = by_partner.get(r["name"].lower(), [])
        active = [e for e in entries if not e["disabled"]]
        if active:
            r["cron_status"] = "found"
        elif entries:
            # Present but commented out - a deliberate disable, worth naming
            # separately from a cron that isn't there at all.
            r["cron_status"] = "disabled"
        elif not collected_servers:
            r["cron_status"] = "unknown"
        elif r.get("server") and r["server"] not in collected_servers:
            # We never looked at that box, so absence proves nothing.
            r["cron_status"] = "unknown"
        elif not r.get("server"):
            r["cron_status"] = "unknown"
        else:
            r["cron_status"] = "missing"
        r["cron_count"] = len(entries)
        r["cron_schedule"] = (active or entries or [{}])[0].get("schedule_human")

    for r in rows:
        r["job_rank"] = jobs_mod.STATE_RANK.get(r.get("job_state"), 9)
    # A stalled partner whose cron is also missing is the most actionable thing
    # on the page, so it sorts above other stalled rows.
    rows.sort(key=lambda r: (
        r["job_rank"],
        0 if r.get("cron_status") == "missing" else 1,
        -(r.get("db_future") or 0),
    ))

    counts = {}
    for r in rows:
        counts[r.get("job_state", "unknown")] = counts.get(r.get("job_state", "unknown"), 0) + 1
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
            "cron_collected": len(collected_servers),
        },
        "jobs": scheduler.status(),
        "now": time.time(),
    }


@app.get("/api/cron")
async def api_cron():
    """Every crontab line collected from the servers, newest write first."""
    rows = store.cron_jobs()
    now = time.time()
    for r in rows:
        r["log_age_days"] = (
            round((now - r["log_mtime"]) / 86400.0, 1) if r["log_mtime"] else None
        )
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
    return {
        "jobs": rows,
        "servers": servers,
        "job": job,
        "source": config.cron_source,
        "summary": {
            "total": len(rows),
            "servers": len(servers),
            "disabled": sum(1 for r in rows if r["disabled"]),
            "with_partner": sum(1 for r in rows if r["partner"]),
            "with_log": sum(1 for r in rows if r["log_mtime"]),
        },
        "now": now,
    }


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
