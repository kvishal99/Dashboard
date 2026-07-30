"""Website up/down checker.

One check = one HTTP request. A site is healthy when the request completes, the
status code is one of the accepted ones, and (optionally) `keyword` appears in
the body. Everything else - timeouts, DNS failures, TLS errors, an unexpected
status - is reported as down with the reason attached.

`expect_status` may be a single code or a list. A list is the honest way to
describe a host that legitimately answers 401 (auth required) or 403 (WAF
blocks this machine): the server is up, and a change away from that code is
the thing worth alerting on.
"""
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx

DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 OpsDashboard/1.0"
)


@dataclass
class CheckResult:
    ok: bool
    status_code: Optional[int]
    latency_ms: Optional[int]
    error: Optional[str]


def _accepted(site: Dict[str, Any]) -> List[int]:
    expect = site.get("expect_status", 200)
    if isinstance(expect, (list, tuple)):
        return [int(c) for c in expect]
    return [int(expect)]


async def check_site(client: httpx.AsyncClient, site: Dict[str, Any]) -> CheckResult:
    url = site["url"]
    accepted = _accepted(site)
    keyword = site.get("keyword")
    timeout = float(site.get("timeout", 10))
    method = (site.get("method") or "GET").upper()

    headers = {"User-Agent": DEFAULT_UA}
    headers.update(site.get("headers") or {})

    started = time.perf_counter()
    try:
        resp = await client.request(
            method,
            url,
            timeout=timeout,
            headers=headers,
            follow_redirects=site.get("follow_redirects", True),
        )
        latency = int((time.perf_counter() - started) * 1000)

        if resp.status_code not in accepted:
            want = " or ".join(str(c) for c in accepted)
            return CheckResult(
                ok=False,
                status_code=resp.status_code,
                latency_ms=latency,
                error=f"expected HTTP {want}, got {resp.status_code}",
            )
        if keyword and keyword not in resp.text:
            return CheckResult(
                ok=False,
                status_code=resp.status_code,
                latency_ms=latency,
                error=f"keyword {keyword!r} not found in response body",
            )
        return CheckResult(ok=True, status_code=resp.status_code, latency_ms=latency, error=None)

    except httpx.TimeoutException:
        latency = int((time.perf_counter() - started) * 1000)
        return CheckResult(
            ok=False, status_code=None, latency_ms=latency, error=f"timeout after {timeout}s"
        )
    except Exception as exc:
        latency = int((time.perf_counter() - started) * 1000)
        return CheckResult(
            ok=False, status_code=None, latency_ms=latency,
            error=f"{type(exc).__name__}: {exc}",
        )
