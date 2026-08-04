"""Background cron-style loops.

Three independent jobs run for the life of the process:

  counts - re-runs the grouped COUNT query for every partner
  health - polls every configured website and records up/down
  crons  - SSHes to each server and re-reads its crontab

They are scheduled two different ways, on purpose:

  * counts is CRON-ALIGNED: it fires on the top of the hour and nowhere else,
    so MySQL sees exactly 24 sweeps in 24 hours regardless of how long a sweep
    takes or how often the process restarts. An interval timer cannot promise
    that - it drifts forward by the run duration every cycle, and every restart
    starts a fresh cycle.
  * health and crons are INTERVAL jobs: they touch no database, so they are
    free to run as often as usefully possible. health is the near-real-time one.

All three can also be triggered on demand from the UI ("Refresh now") via
run_counts() / run_health(). A job never runs twice concurrently: the on-demand
path and the timer share a lock, so hitting Refresh during a scheduled run
waits rather than doubling the load on MySQL.
"""
import asyncio
import os
import time
from typing import Any, Dict, List, Optional

import httpx

import counts
import cron_collect
from alerts import Alerter
from config import Config
from health import check_site
from store import Store


def seconds_to_next_hour(now: Optional[float] = None) -> float:
    """Seconds from `now` until the next top of the local hour.

    Local rather than UTC-modulo: on a +05:30 offset, aligning by `now % 3600`
    would fire at HH:30, which reads as a drifting clock to anyone watching.
    """
    now = time.time() if now is None else now
    tm = time.localtime(now)
    into_hour = tm.tm_min * 60 + tm.tm_sec
    return max(1.0, 3600.0 - into_hour)


class JobState:
    def __init__(self, name: str, interval: int, schedule: str = "interval"):
        self.name = name
        self.interval = interval
        # "interval" - every `interval` seconds; "hourly" - on the hour, 24x/day.
        self.schedule = schedule
        self.lock = asyncio.Lock()
        self.last_run: Optional[float] = None
        self.last_duration_ms: Optional[int] = None
        self.next_run: Optional[float] = None
        self.running: bool = False
        self.progress: str = ""
        self.last_error: Optional[str] = None
        self.runs: int = 0
        # Timestamps of the last 24h of runs. For the hourly job this is the
        # number the whole design is about, so the UI gets to show it rather
        # than asking anyone to trust the schedule.
        self.run_times: List[float] = []

    def note_run(self, when: float) -> None:
        self.run_times.append(when)
        cutoff = when - 86400
        self.run_times = [t for t in self.run_times if t >= cutoff]

    def runs_24h(self) -> int:
        cutoff = time.time() - 86400
        return sum(1 for t in self.run_times if t >= cutoff)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "schedule": self.schedule,
            "interval_seconds": self.interval,
            "last_run": self.last_run,
            "last_duration_ms": self.last_duration_ms,
            "next_run": self.next_run,
            "running": self.running,
            "progress": self.progress,
            "last_error": self.last_error,
            "runs": self.runs,
            "runs_24h": self.runs_24h(),
        }


class Scheduler:
    def __init__(self, config: Config, store: Store):
        self.config = config
        self.store = store
        self.jobs = {
            "counts": JobState(
                "counts", config.counts_interval,
                schedule="hourly" if config.counts_hourly else "interval",
            ),
            "health": JobState("health", config.health_interval),
            "crons": JobState("crons", config.cron_interval),
        }
        self._tasks: List[asyncio.Task] = []
        self._sem = asyncio.Semaphore(config.max_concurrent_queries)
        # Turns the stream of check results into the few emails a human wants.
        self.alerter = Alerter(config.alerts, store)

    # -------------------------------------------------------------- lifecycle

    def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._loop("counts", self.run_counts)),
            asyncio.create_task(self._prune_loop()),
        ]
        # Not started at all when site checks are off, so there is no timer that
        # could fire a request. run_health refuses independently of this.
        if self.config.health_checks_enabled:
            self._tasks.append(asyncio.create_task(self._loop("health", self.run_health)))
        # Started only in ssh mode. With cron_source: agent the servers push
        # their crontabs to /api/cron/report and this process never opens an SSH
        # session - which is the point: no stored passwords, no askpass, and
        # nothing to prompt on a terminal.
        if self.config.cron_source == "ssh":
            self._tasks.append(asyncio.create_task(self._loop("crons", self.run_crons)))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks = []

    async def _loop(self, job_name: str, fn) -> None:
        job = self.jobs[job_name]
        # Health first (fast, so the sites tab fills immediately), then counts,
        # then crons last - it holds SSH sessions open and is the least urgent.
        if job_name == "counts":
            await asyncio.sleep(3)
        elif job_name == "crons":
            await asyncio.sleep(10)

        if job.schedule == "hourly":
            await self._hourly_loop(job, fn)
            return

        while True:
            await self._run_once(job, fn)
            job.next_run = time.time() + job.interval
            await asyncio.sleep(job.interval)

    async def _hourly_loop(self, job, fn) -> None:
        """Fire on the top of every hour - 24 runs per day, no drift.

        The startup run is conditional. Restarting the process must not buy an
        extra sweep: if the stored data is already less than an hour old, we
        just wait for the next boundary like nothing happened. Only a cold or
        long-stopped dashboard collects immediately.
        """
        age = self._last_counts_age()
        if age is None or age >= 3600:
            job.progress = "catching up on startup"
            await self._run_once(job, fn)
        while True:
            delay = seconds_to_next_hour()
            job.next_run = time.time() + delay
            await asyncio.sleep(delay)
            await self._run_once(job, fn)

    async def _run_once(self, job, fn) -> None:
        """Run a job body, absorbing failures so one bad cycle can't kill the loop."""
        try:
            await fn()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            job.last_error = f"{type(exc).__name__}: {exc}"

    async def _prune_loop(self) -> None:
        """Drop history older than the retention window, once a day.

        Store.prune() existed but nothing called it. It earns its keep now that
        health runs every 30s: that is ~14k site_checks rows a day, and both the
        24h uptime figure and the sparklines read that table on every page poll.
        """
        while True:
            await asyncio.sleep(86400)
            try:
                await asyncio.to_thread(self.store.prune, self.config.history_keep_days)
                await asyncio.to_thread(self._prune_exports)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass  # housekeeping must never take the scheduler down

    def _prune_exports(self) -> None:
        """Delete generated event CSVs past their retention window.

        These are the only large files the dashboard writes - one wcities
        export is 120 MB - and every one of them is reproducible from the
        database, so keeping them indefinitely buys nothing but disk. The row
        goes with the file: a listing that offers a download which 404s is
        worse than no listing.
        """
        cutoff = time.time() - self.config.export_keep_days * 86400
        for row in self.store.exports(limit=1000):
            stamp = row.get("finished_at") or row.get("requested_at") or 0
            if row.get("status") in ("queued", "running") or stamp >= cutoff:
                continue
            path = row.get("stored_path")
            if path and os.path.isfile(path):
                try:
                    os.remove(path)
                except OSError:
                    pass
            self.store.delete_export(row["id"])

    def _last_counts_age(self) -> Optional[float]:
        """Seconds since the newest stored count, or None if we have none."""
        try:
            last = self.store.last_counts_at()
        except Exception:
            return None
        return None if last is None else max(0.0, time.time() - last)

    # ----------------------------------------------------------------- counts

    async def run_counts(self, partner_name: Optional[str] = None) -> Dict[str, Any]:
        """Refresh counts.

        With no argument this is ONE grouped query covering every partner in the
        database - that's the scheduled path. Pass partner_name to re-count just
        one partner (the per-row Refresh button), which uses the cheaper
        single-partner queries.
        """
        job = self.jobs["counts"]
        async with job.lock:
            job.running = True
            started = time.perf_counter()
            try:
                if partner_name:
                    job.progress = f"re-counting {partner_name}"
                    result = await asyncio.to_thread(
                        counts.collect, partner_name,
                        self.config.database, self.config.queries,
                    )
                    self.store.record_counts(
                        partner=partner_name,
                        feed_total=result.feed_total,
                        db_future=result.db_future,
                        db_past=result.db_past,
                        ok=result.ok,
                        error=result.error,
                        duration_ms=result.duration_ms,
                    )
                    job.last_error = None
                    return {"partners": 1}

                job.progress = "scanning all partners"
                sweep = await asyncio.to_thread(
                    counts.collect_all, self.config.database, self.config.queries
                )
                if not sweep.ok:
                    # One failure covers every partner, so record it against the
                    # ones we already know about rather than losing it silently.
                    job.last_error = sweep.error
                    for name in self.store.latest_counts():
                        self.store.record_counts(
                            partner=name, feed_total=None, db_future=None,
                            ok=False, error=sweep.error,
                            duration_ms=sweep.duration_ms,
                        )
                    return {"partners": 0, "error": sweep.error}

                kept = 0
                for name, res in sweep.partners.items():
                    if self.config.is_excluded(name):
                        continue
                    if (res.db_future or 0) < self.config.min_live_events:
                        continue
                    self.store.record_counts(
                        partner=name,
                        feed_total=res.feed_total,
                        db_future=res.db_future,
                        db_past=res.db_past,
                        last_created=res.last_created,
                        ok=True,
                        duration_ms=sweep.duration_ms,
                    )
                    kept += 1
                job.last_error = None
                job.progress = ""
                return {"partners": kept, "seen": len(sweep.partners)}
            finally:
                job.running = False
                job.progress = ""
                job.last_run = time.time()
                job.last_duration_ms = int((time.perf_counter() - started) * 1000)
                job.runs += 1
                job.note_run(job.last_run)

    # ----------------------------------------------------------------- health

    async def run_health(self, url: Optional[str] = None) -> Dict[str, Any]:
        """Check websites. Pass url to check a single site.

        Returns without opening a client when health_checks_enabled is false.
        The guard lives here rather than only at the call sites because this is
        the single place an outbound request to a monitored site is made.
        """
        if not self.config.health_checks_enabled:
            return {
                "checked": 0,
                "disabled": True,
                "reason": "site checks are disabled (app.health_checks_enabled: false) - "
                          "no HTTP request was made",
            }
        job = self.jobs["health"]
        async with job.lock:
            job.running = True
            started = time.perf_counter()
            sites = [w for w in self.config.websites if url is None or w["url"] == url]
            try:
                async with httpx.AsyncClient() as client:
                    results = await asyncio.gather(
                        *(check_site(client, site) for site in sites)
                    )
                for site, res in zip(sites, results):
                    self.store.record_check(
                        site_name=site["name"],
                        url=site["url"],
                        ok=res.ok,
                        status_code=res.status_code,
                        latency_ms=res.latency_ms,
                        error=res.error,
                    )
                # Mail on state changes. evaluate() swallows its own errors, so a
                # dead SMTP server slows nothing down and stops no checks.
                for site, res in zip(sites, results):
                    await self.alerter.evaluate(site, res)
                job.last_error = None
            finally:
                job.running = False
                job.last_run = time.time()
                job.last_duration_ms = int((time.perf_counter() - started) * 1000)
                job.runs += 1
                job.note_run(job.last_run)
        return {"checked": len(sites)}

    # ----------------------------------------------------------------- crons

    async def run_crons(self) -> Dict[str, Any]:
        """SSH to each server and refresh its crontab listing.

        On a long interval by default: it opens an SSH session per host and stats
        every redirect target, so it is far heavier than the other jobs while the
        answer changes rarely.
        """
        job = self.jobs["crons"]
        async with job.lock:
            job.running = True
            job.progress = "connecting to servers"
            started = time.perf_counter()
            try:
                result = await asyncio.to_thread(
                    cron_collect.collect, self.config, self.store,
                    None, False, lambda m: setattr(job, "progress", m),
                )
                unreachable = [h for h in result["hosts"] if not h["ok"]]
                # Some hosts being unreachable is the normal state here (three
                # partner boxes have no route at all), so it is only an error
                # when nothing at all could be collected.
                job.last_error = (
                    "; ".join(f"{h['host']}: {h['error']}" for h in unreachable)
                    if unreachable and not result["reachable"] else None
                )
                return result
            finally:
                job.running = False
                job.progress = ""
                job.last_run = time.time()
                job.last_duration_ms = int((time.perf_counter() - started) * 1000)
                job.runs += 1
                job.note_run(job.last_run)

    # ----------------------------------------------------------------- status

    def status(self) -> Dict[str, Any]:
        return {name: job.as_dict() for name, job in self.jobs.items()}
