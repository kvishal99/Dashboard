"""Loads config.yaml (plus .env / environment overrides)."""
import os
from typing import Any, Dict, List

import yaml

from alerts import AlertSettings, SmtpSettings

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get("OPS_CONFIG", os.path.join(BASE_DIR, "config.yaml"))


def _find_env() -> str:
    """Locate .env beside the code, or one level up.

    The parent-directory fallback matters because the code can live in a
    subdirectory (e.g. a git repo checked out as ./Dashboard) while .env stays
    outside it, deliberately un-committed.
    """
    candidates = [
        os.environ.get("OPS_ENV_FILE"),
        os.path.join(BASE_DIR, ".env"),
        os.path.join(os.path.dirname(BASE_DIR), ".env"),
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return os.path.join(BASE_DIR, ".env")


ENV_PATH = _find_env()

APP_DEFAULTS = {
    "agent_secret": "change-this-to-a-secure-token",
    # The DB sweep runs on the hour, so this is only the fallback cadence used
    # when counts_hourly is turned off.
    "counts_interval_seconds": 3600,
    "counts_hourly": True,
    "health_interval_seconds": 30,
    # "agent" - servers push their own crontabs to /api/cron/report (no SSH).
    # "ssh"   - the dashboard logs into each server and reads them itself.
    "cron_source": "agent",
    "cron_interval_seconds": 21600,
    "pm2_stale_seconds": 30,
    "db_path": "ops.db",
    "history_keep_days": 90,
    "max_concurrent_queries": 4,
}

# config.yaml key -> environment variable that overrides it.
DB_ENV = {
    "host": "OPS_DB_HOST",
    "port": "OPS_DB_PORT",
    "user": "OPS_DB_USER",
    "password": "OPS_DB_PASSWORD",
    "database": "OPS_DB_NAME",
}


def _load_dotenv(path: str = ENV_PATH) -> None:
    """Minimal .env reader - existing environment variables always win."""
    if not os.path.isfile(path):
        return
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


def _split_addresses(value: str) -> List[str]:
    """Split a comma/space separated address list, dropping blanks."""
    return [a.strip() for a in value.replace(";", ",").replace(" ", ",").split(",") if a.strip()]


def _build_alerts(raw: Dict[str, Any]) -> AlertSettings:
    """Email alert settings from config.yaml, with environment overrides.

    The SMTP password never belongs in config.yaml, so it is read from the
    environment / .env only (OPS_SMTP_PASSWORD). Everything else can be set
    either way, environment winning, which is what lets a server override the
    recipient list without editing a committed file.
    """
    smtp_raw = raw.get("smtp") or {}
    smtp = SmtpSettings(
        host=os.environ.get("OPS_SMTP_HOST") or str(smtp_raw.get("host") or "").strip(),
        port=int(os.environ.get("OPS_SMTP_PORT") or smtp_raw.get("port") or 587),
        security=(
            os.environ.get("OPS_SMTP_SECURITY") or smtp_raw.get("security") or "starttls"
        ).strip().lower(),
        username=os.environ.get("OPS_SMTP_USER") or str(smtp_raw.get("username") or "").strip(),
        # Password: environment only, on purpose.
        password=os.environ.get("OPS_SMTP_PASSWORD", ""),
        timeout=int(smtp_raw.get("timeout") or 20),
    )

    recipients = [str(r).strip() for r in (raw.get("recipients") or []) if str(r).strip()]
    if os.environ.get("OPS_ALERT_TO"):
        recipients = _split_addresses(os.environ["OPS_ALERT_TO"])

    enabled = raw.get("enabled", True)
    if os.environ.get("OPS_ALERTS_ENABLED"):
        enabled = os.environ["OPS_ALERTS_ENABLED"].strip().lower() in ("1", "true", "yes")

    return AlertSettings(
        enabled=bool(enabled),
        recipients=recipients,
        from_address=(
            os.environ.get("OPS_ALERT_FROM")
            or str(raw.get("from_address") or "").strip()
            # A sane default: authenticated SMTP usually requires From to be the
            # account itself, so fall back to the login rather than to nothing.
            or smtp.username
        ),
        from_name=str(raw.get("from_name") or "Ops Dashboard"),
        failures_before_alert=max(1, int(raw.get("failures_before_alert") or 2)),
        successes_before_recovery=max(1, int(raw.get("successes_before_recovery") or 2)),
        repeat_hours=float(raw.get("repeat_hours", 6)),
        send_recovery=bool(raw.get("send_recovery", True)),
        dashboard_url=str(raw.get("dashboard_url") or "https://monitor.wcities.com/sites"),
        smtp=smtp,
    )


class Config:
    def __init__(self, raw: Dict[str, Any]):
        self.raw = raw
        app = {**APP_DEFAULTS, **(raw.get("app") or {})}
        # The shared secret belongs in .env, not in config.yaml.
        self.agent_secret: str = os.environ.get("OPS_AGENT_SECRET") or app["agent_secret"]
        self.counts_interval: int = int(app["counts_interval_seconds"])
        # True = fire on the top of every hour (24 MySQL sweeps a day, no drift).
        # False = plain timer on counts_interval_seconds.
        self.counts_hourly: bool = bool(app["counts_hourly"])
        self.health_interval: int = int(app["health_interval_seconds"])
        self.cron_interval: int = int(app["cron_interval_seconds"])
        # Only "ssh" makes the dashboard log into anything. Anything else means
        # the agents push, and no SSH job is started at all.
        self.cron_source: str = str(app["cron_source"]).strip().lower()
        self.pm2_stale_seconds: int = int(app["pm2_stale_seconds"])
        self.max_concurrent_queries: int = int(app["max_concurrent_queries"])
        self.history_keep_days: int = int(app["history_keep_days"])

        db_path = app["db_path"]
        self.db_path = db_path if os.path.isabs(db_path) else os.path.join(BASE_DIR, db_path)

        self.database: Dict[str, Any] = dict(raw.get("database") or {})
        for key, env_var in DB_ENV.items():
            if os.environ.get(env_var):
                self.database[key] = os.environ[env_var]
        self.database["port"] = int(self.database.get("port", 3306))

        self.queries: Dict[str, str] = raw.get("queries") or {}
        self.websites: List[Dict[str, Any]] = raw.get("websites") or []
        self.alerts: AlertSettings = _build_alerts(raw.get("alerts") or {})

        # Partners are discovered from the database, not listed here.
        discovery = raw.get("partner_discovery") or {}
        self.min_live_events: int = int(discovery.get("min_live_events", 1))
        # Matched case-insensitively: the sheet writes "Vegas", MySQL stores "vegas".
        self.excluded: set = {str(n).strip().lower() for n in (discovery.get("exclude") or [])}
        self.partner_meta: Dict[str, Dict[str, Any]] = raw.get("partner_meta") or {}
        self._meta_lower = {k.lower(): v for k, v in self.partner_meta.items()}

        self._validate()

    def is_excluded(self, partner: str) -> bool:
        return partner.strip().lower() in self.excluded

    def meta_for(self, partner: str) -> Dict[str, Any]:
        """Cosmetic extras from the status sheet. Missing is fine."""
        return self._meta_lower.get(partner.strip().lower(), {})

    def _validate(self) -> None:
        for key in ("all_partners", "feed_total", "db_future", "db_past"):
            if not self.queries.get(key):
                raise ValueError(f"config.yaml: queries.{key} is required")

        for key in ("host", "user", "database"):
            if not self.database.get(key):
                raise ValueError(f"config.yaml: database.{key} is required")

        for w in self.websites:
            if not w.get("url"):
                raise ValueError("every website needs a `url`")
            w.setdefault("name", w["url"])
            w.setdefault("expect_status", 200)
            w.setdefault("timeout", 10)

    @property
    def has_db_password(self) -> bool:
        return bool(self.database.get("password"))


def load_config(path: str = CONFIG_PATH) -> Config:
    _load_dotenv()
    with open(path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    return Config(raw)
