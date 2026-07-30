"""Background cron-style loops.

Two independent jobs run for the life of the process:

  counts - re-runs both COUNT queries for every partner
  health - polls every configured website and records up/down

Both can also be triggered on demand from the UI ("Refresh now") via
run_counts() / run_health(). A job never runs twice concurrently: the on-demand
path and the timer share a lock, so hitting Refresh during a scheduled run
waits rather than doubling the load on MySQL.
"""
import asyncio
import time
from typing import Any, Dict, List, Optional

import httpx

import counts
from config import Config
from health import check_site
from store import Store


class JobState:
    def __init__(self, name: str, interval: int):
        self.name = name
        self.interval = interval
        self.lock = asyncio.Lock()
        self.last_run: Optional[float] = None
        self.last_duration_ms: Optional[int] = None
        self.next_run: Optional[float] = None
        self.running: bool = False
        self.progress: str = ""
        self.last_error: Optional[str] = None
        self.runs: int = 0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "interval_seconds": self.interval,
            "last_run": self.last_run,
            "last_duration_ms": self.last_duration_ms,
            "next_run": self.next_run,
            "running": self.running,
            "progress": self.progress,
            "last_error": self.last_error,
            "runs": self.runs,
        }


class Scheduler:
    def __init__(self, config: Config, store: Store):
        self.config = config
        self.store = store
        self.jobs = {
            "counts": JobState("counts", config.counts_interval),
            "health": JobState("health", config.health_interval),
        }
        self._tasks: List[asyncio.Task] = []
        self._sem = asyncio.Semaphore(config.max_concurrent_queries)

    # -------------------------------------------------------------- lifecycle

    def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._loop("counts", self.run_counts)),
            asyncio.create_task(self._loop("health", self.run_health)),
        ]

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
        # Health first: it's fast, so the sites tab fills in while the slower
        # count queries are still running.
        if job_name == "counts":
            await asyncio.sleep(3)
        while True:
            try:
                await fn()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # one bad cycle must not kill the loop
                job.last_error = f"{type(exc).__name__}: {exc}"
            job.next_run = time.time() + job.interval
            await asyncio.sleep(job.interval)

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

    # ----------------------------------------------------------------- health

    async def run_health(self, url: Optional[str] = None) -> Dict[str, Any]:
        """Check websites. Pass url to check a single site."""
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
                job.last_error = None
            finally:
                job.running = False
                job.last_run = time.time()
                job.last_duration_ms = int((time.perf_counter() - started) * 1000)
                job.runs += 1
        return {"checked": len(sites)}

    # ----------------------------------------------------------------- status

    def status(self) -> Dict[str, Any]:
        return {name: job.as_dict() for name, job in self.jobs.items()}
