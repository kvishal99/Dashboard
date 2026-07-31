"""Email alerts for sites that go down.

The health job already knows, every 30 seconds, whether each site answered. This
module turns that stream of yes/no into the small number of emails a human
actually wants:

  * DOWN     - sent once, after `failures_before_alert` checks in a row failed.
               One failed check is not an outage: a single timeout, a WAF hiccup
               or a dropped packet would otherwise mail four people at 3am.
  * REMINDER - while a site stays down, one repeat every `repeat_hours`, so a
               long outage doesn't fall off the bottom of an inbox.
  * RECOVERED- sent once when the site answers `successes_before_recovery` times
               in a row again, with how long it was down.

Nothing else is ever sent. A flapping site produces at most one DOWN and one
RECOVERED per flap, not one mail per check.

The up/down state per site lives in SQLite, not just in memory, so restarting
the dashboard cannot re-send a DOWN mail for an outage it already reported.
Streaks are in memory only - after a restart a site has to fail
`failures_before_alert` times again before anything is sent, which is the
conservative direction.

Sending is best-effort by design: SMTP failures are recorded and shown on the
Sites page, but they never take the health loop down. A monitoring tool that
dies because its mail server is unreachable is worse than useless.
"""
import asyncio
import smtplib
import ssl
import time
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formatdate
from typing import Any, Dict, List, Optional

from health import CheckResult


def _fmt_duration(seconds: Optional[float]) -> str:
    if seconds is None or seconds < 0:
        return "unknown"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    hours, rest = divmod(seconds, 3600)
    return f"{hours}h {rest // 60}m"


def _fmt_time(ts: Optional[float]) -> str:
    if not ts:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(ts))


@dataclass
class SmtpSettings:
    host: str = ""
    port: int = 587
    # starttls - plain connection upgraded to TLS (port 587, the usual one)
    # ssl      - TLS from the first byte (port 465)
    # none     - no encryption; only sane for a relay on localhost
    security: str = "starttls"
    username: str = ""
    password: str = ""
    timeout: int = 20

    @property
    def configured(self) -> bool:
        return bool(self.host)


@dataclass
class AlertSettings:
    enabled: bool = True
    recipients: List[str] = field(default_factory=list)
    from_address: str = ""
    from_name: str = "Ops Dashboard"
    # Checks run every 30s, so 2 failures = roughly a minute down before mailing.
    failures_before_alert: int = 2
    successes_before_recovery: int = 2
    # Reminder cadence while a site stays down. 0 disables reminders entirely.
    repeat_hours: float = 6.0
    send_recovery: bool = True
    # Link put at the bottom of every mail so the reader can open the tab.
    dashboard_url: str = "https://monitor.wcities.com/sites"
    smtp: SmtpSettings = field(default_factory=SmtpSettings)

    @property
    def ready(self) -> bool:
        """Everything needed to actually deliver a mail is present."""
        return bool(
            self.enabled
            and self.recipients
            and self.from_address
            and self.smtp.configured
        )

    def why_not_ready(self) -> Optional[str]:
        if not self.enabled:
            return "alerts.enabled is false in config.yaml"
        if not self.smtp.configured:
            return "no SMTP host set (alerts.smtp.host or OPS_SMTP_HOST)"
        if not self.from_address:
            return "no from address set (alerts.from_address or OPS_ALERT_FROM)"
        if not self.recipients:
            return "no recipients set (alerts.recipients or OPS_ALERT_TO)"
        return None


def send_email(settings: AlertSettings, subject: str, body: str) -> None:
    """Send one mail, synchronously. Raises on failure - the caller records it."""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{settings.from_name} <{settings.from_address}>"
    msg["To"] = ", ".join(settings.recipients)
    msg["Date"] = formatdate(localtime=True)
    msg.set_content(body)

    smtp = settings.smtp
    security = (smtp.security or "starttls").lower()

    if security == "ssl":
        server = smtplib.SMTP_SSL(
            smtp.host, smtp.port, timeout=smtp.timeout, context=ssl.create_default_context()
        )
    else:
        server = smtplib.SMTP(smtp.host, smtp.port, timeout=smtp.timeout)

    try:
        server.ehlo()
        if security == "starttls":
            server.starttls(context=ssl.create_default_context())
            server.ehlo()
        if smtp.username:
            server.login(smtp.username, smtp.password)
        server.send_message(msg)
    finally:
        try:
            server.quit()
        except Exception:
            pass


class Alerter:
    """Decides which check results deserve an email, and sends them."""

    def __init__(self, settings: AlertSettings, store: Any):
        self.settings = settings
        self.store = store
        # url -> consecutive failures / successes since the last state change.
        self._fail_streak: Dict[str, int] = {}
        self._ok_streak: Dict[str, int] = {}
        # Surfaced on the Sites page so a broken mail setup is visible rather
        # than silently swallowing every alert.
        self.last_error: Optional[str] = None
        self.last_sent_at: Optional[float] = None
        self.sent_count: int = 0

    # ------------------------------------------------------------------ status

    def status(self) -> Dict[str, Any]:
        return {
            "enabled": self.settings.enabled,
            "ready": self.settings.ready,
            "reason": self.settings.why_not_ready(),
            "recipients": self.settings.recipients,
            "failures_before_alert": self.settings.failures_before_alert,
            "last_sent_at": self.last_sent_at,
            "last_error": self.last_error,
            "sent_count": self.sent_count,
        }

    # ------------------------------------------------------------- evaluation

    async def evaluate(self, site: Dict[str, Any], result: CheckResult) -> None:
        """Fold one check result into the site's alert state, mailing if needed.

        Called once per site per health run. Never raises: a failure here must
        not stop the health job from recording the check it just made.
        """
        try:
            await self._evaluate(site, result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"

    async def _evaluate(self, site: Dict[str, Any], result: CheckResult) -> None:
        if not self.settings.enabled:
            return

        url = site["url"]
        name = site.get("name") or url
        now = time.time()
        state = self.store.alert_state(url) or {"state": "up", "since": now, "last_sent": None}
        current = state.get("state") or "up"

        if result.ok:
            self._fail_streak[url] = 0
            self._ok_streak[url] = self._ok_streak.get(url, 0) + 1

            if current == "down" and self._ok_streak[url] >= self.settings.successes_before_recovery:
                down_since = state.get("since") or now
                self.store.save_alert_state(url, "up", since=now, last_sent=now)
                if self.settings.send_recovery:
                    await self._send(
                        url, name, "recovery",
                        subject=f"[RECOVERED] {name} is back up",
                        body=self._recovery_body(site, result, down_since, now),
                    )
            return

        # ---- failed check ----
        self._ok_streak[url] = 0
        self._fail_streak[url] = self._fail_streak.get(url, 0) + 1
        fails = self._fail_streak[url]

        if current != "down":
            if fails < self.settings.failures_before_alert:
                return  # not an outage yet, just a bad check
            self.store.save_alert_state(url, "down", since=now, last_sent=now)
            await self._send(
                url, name, "down",
                subject=f"[DOWN] {name} is not responding",
                body=self._down_body(site, result, fails, now, now),
            )
            return

        # Already known to be down - only a periodic reminder is left.
        if self.settings.repeat_hours <= 0:
            return
        last_sent = state.get("last_sent") or state.get("since") or now
        if now - last_sent < self.settings.repeat_hours * 3600:
            return
        down_since = state.get("since") or now
        self.store.save_alert_state(url, "down", since=down_since, last_sent=now)
        await self._send(
            url, name, "reminder",
            subject=f"[STILL DOWN] {name} - down for {_fmt_duration(now - down_since)}",
            body=self._down_body(site, result, fails, down_since, now),
        )

    # ----------------------------------------------------------------- bodies

    def _detail_lines(self, site: Dict[str, Any], result: CheckResult) -> List[Optional[str]]:
        """Lines shared by DOWN and STILL DOWN. None means "omit this line" -
        an empty string would drop the deliberate blank separators too."""
        expect = site.get("expect_status", 200)
        expect = ", ".join(str(c) for c in expect) if isinstance(expect, (list, tuple)) else expect
        return [
            f"URL          : {site['url']}",
            f"Expected     : HTTP {expect}",
            f"Got          : {result.status_code if result.status_code is not None else 'no response'}",
            f"Reason       : {result.error or '-'}",
            f"Response time: {result.latency_ms}ms" if result.latency_ms is not None else None,
        ]

    def _down_body(self, site, result, fails, down_since, now) -> str:
        lines = [
            f"{site.get('name') or site['url']} is failing its health check.",
            "",
            *self._detail_lines(site, result),
            f"Failed checks: {fails} in a row",
            f"Down since   : {_fmt_time(down_since)} ({_fmt_duration(now - down_since)})",
            f"Checked at   : {_fmt_time(now)}",
            "",
            f"Dashboard: {self.settings.dashboard_url}",
        ]
        return "\n".join(l for l in lines if l is not None)

    def _recovery_body(self, site, result, down_since, now) -> str:
        lines = [
            f"{site.get('name') or site['url']} is responding normally again.",
            "",
            f"URL          : {site['url']}",
            f"Status       : HTTP {result.status_code}",
            f"Response time: {result.latency_ms}ms" if result.latency_ms is not None else None,
            f"Was down for : {_fmt_duration(now - down_since)}"
            f" (from {_fmt_time(down_since)} to {_fmt_time(now)})",
            "",
            f"Dashboard: {self.settings.dashboard_url}",
        ]
        return "\n".join(l for l in lines if l is not None)

    # ------------------------------------------------------------------ send

    async def _send(self, url: str, name: str, kind: str, subject: str, body: str) -> None:
        reason = self.settings.why_not_ready()
        if reason:
            # Log the alert that could not go out, so the outage is still on
            # record and the missing configuration is obvious.
            self.last_error = f"not sent - {reason}"
            self.store.record_alert(url, name, kind, self.settings.recipients,
                                    ok=False, error=self.last_error, subject=subject)
            return

        try:
            # smtplib is blocking, so it goes to a thread - the health loop is
            # async and must not stall on a slow mail server.
            await asyncio.to_thread(send_email, self.settings, subject, body)
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            self.store.record_alert(url, name, kind, self.settings.recipients,
                                    ok=False, error=self.last_error, subject=subject)
            return

        self.last_error = None
        self.last_sent_at = time.time()
        self.sent_count += 1
        self.store.record_alert(url, name, kind, self.settings.recipients,
                                ok=True, error=None, subject=subject)

    async def send_test(self) -> Dict[str, Any]:
        """Send a test mail to every recipient - used by /api/alerts/test."""
        reason = self.settings.why_not_ready()
        if reason:
            return {"ok": False, "error": reason, "recipients": self.settings.recipients}

        body = "\n".join([
            "This is a test alert from the Ops Dashboard.",
            "",
            "If you received this, down/recovery alerts for the monitored sites",
            "will reach this address.",
            "",
            f"Sent at  : {_fmt_time(time.time())}",
            f"Dashboard: {self.settings.dashboard_url}",
        ])
        try:
            await asyncio.to_thread(
                send_email, self.settings, "[TEST] Ops Dashboard alert test", body
            )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            self.last_error = error
            self.store.record_alert("-", "test", "test", self.settings.recipients,
                                    ok=False, error=error, subject="test")
            return {"ok": False, "error": error, "recipients": self.settings.recipients}

        self.last_error = None
        self.last_sent_at = time.time()
        self.sent_count += 1
        self.store.record_alert("-", "test", "test", self.settings.recipients,
                                ok=True, error=None, subject="test")
        return {"ok": True, "recipients": self.settings.recipients}
