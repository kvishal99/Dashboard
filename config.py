"""Loads config.yaml (plus .env / environment overrides)."""
import os
from typing import Any, Dict, List, Tuple

import yaml

import event_export
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
    # HTTP Basic auth on every page. The password is read from .env only
    # (OPS_AUTH_PASSWORD); an empty one leaves the dashboard open, which is
    # the right default for a laptop but must be set on anything reachable.
    "auth_user": "admin",
    # The DB sweep runs on the hour, so this is only the fallback cadence used
    # when counts_hourly is turned off.
    "counts_interval_seconds": 3600,
    "counts_hourly": True,
    "health_interval_seconds": 30,
    # False stops the dashboard making ANY outbound HTTP request to a monitored
    # site - the scheduled loop never starts and the manual refresh refuses. The
    # sites tab then shows the last stored result and pm2 becomes the live
    # signal. Set when the sites must not be hit or crawled from here at all.
    "health_checks_enabled": True,
    # "agent" - servers push their own crontabs to /api/cron/report (no SSH).
    # "ssh"   - the dashboard logs into each server and reads them itself.
    "cron_source": "agent",
    "cron_interval_seconds": 21600,
    "pm2_stale_seconds": 30,
    "db_path": "ops.db",
    "history_keep_days": 90,
    "max_concurrent_queries": 4,
    # Spreadsheet comparison. max_compare_rows is a refusal threshold, not a
    # truncation point - see compare.py for why a partial diff is worse than none.
    "upload_dir": "uploads",
    "max_upload_mb": 25,
    "max_compare_rows": 100_000,
    # Generated per-partner event CSVs. Unlike the comparison cap above,
    # max_export_rows is a truncation point rather than a refusal: a partial
    # event export is still a usable file, and the UI states the row count.
    "export_dir": "exports",
    "export_keep_days": 7,
    "max_export_rows": 0,
    # The dashboard's own log, read per partner on the partner's Logs tab.
    "log_file": "dashboard.log",
    "log_level": "INFO",
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


def _event_columns(raw: Dict[str, Any]) -> List[Tuple[str, str]]:
    """The event CSV's columns, as (field, header) pairs.

    Accepts either a two-item list (`[id, "Event ID"]`) or a bare string, in
    which case the field name doubles as the header. A malformed or absent
    block falls back to the default rather than producing a file with no
    columns, because an empty CSV looks like "the partner has no events".
    """
    configured = raw.get("event_columns")
    if not configured:
        return list(event_export.DEFAULT_COLUMNS)

    columns: List[Tuple[str, str]] = []
    for entry in configured:
        if isinstance(entry, str):
            columns.append((entry, entry))
        elif isinstance(entry, (list, tuple)) and entry:
            key = str(entry[0])
            columns.append((key, str(entry[1]) if len(entry) > 1 else key))
    return columns or list(event_export.DEFAULT_COLUMNS)


class Config:
    def __init__(self, raw: Dict[str, Any]):
        self.raw = raw
        app = {**APP_DEFAULTS, **(raw.get("app") or {})}
        # The shared secret belongs in .env, not in config.yaml.
        self.agent_secret: str = os.environ.get("OPS_AGENT_SECRET") or app["agent_secret"]
        # Login for the UI. Username may live in config.yaml; the password is
        # environment/.env only, on purpose - same rule as SMTP and MySQL.
        self.auth_user: str = os.environ.get("OPS_AUTH_USER") or str(app["auth_user"])
        self.auth_password: str = os.environ.get("OPS_AUTH_PASSWORD", "")
        self.counts_interval: int = int(app["counts_interval_seconds"])
        # True = fire on the top of every hour (24 MySQL sweeps a day, no drift).
        # False = plain timer on counts_interval_seconds.
        self.counts_hourly: bool = bool(app["counts_hourly"])
        self.health_interval: int = int(app["health_interval_seconds"])
        # The one switch that decides whether this process ever talks to a
        # monitored site. Checked in run_health itself, not only where the loop
        # is started, so no code path - manual refresh included - can get out.
        self.health_checks_enabled: bool = bool(app["health_checks_enabled"])
        self.cron_interval: int = int(app["cron_interval_seconds"])
        # Only "ssh" makes the dashboard log into anything. Anything else means
        # the agents push, and no SSH job is started at all.
        self.cron_source: str = str(app["cron_source"]).strip().lower()
        self.pm2_stale_seconds: int = int(app["pm2_stale_seconds"])
        self.max_concurrent_queries: int = int(app["max_concurrent_queries"])
        self.history_keep_days: int = int(app["history_keep_days"])

        db_path = app["db_path"]
        self.db_path = db_path if os.path.isabs(db_path) else os.path.join(BASE_DIR, db_path)

        # Uploaded partner sheets, and the dashboard's own log. Both resolve
        # relative to the code, matching db_path, so a relative value in
        # config.yaml behaves the same wherever the process was started from.
        self.upload_dir: str = self._resolve(app["upload_dir"])
        self.max_upload_bytes: int = int(float(app["max_upload_mb"]) * 1024 * 1024)
        self.max_compare_rows: int = int(app["max_compare_rows"])
        self.export_dir: str = self._resolve(app["export_dir"])
        self.export_keep_days: int = int(app["export_keep_days"])
        self.max_export_rows: int = int(app["max_export_rows"])
        self.log_file: str = self._resolve(app["log_file"]) if app["log_file"] else ""
        self.log_level: str = str(app["log_level"]).upper()

        self.database: Dict[str, Any] = dict(raw.get("database") or {})
        for key, env_var in DB_ENV.items():
            if os.environ.get(env_var):
                self.database[key] = os.environ[env_var]
        self.database["port"] = int(self.database.get("port", 3306))

        self.queries: Dict[str, str] = raw.get("queries") or {}

        # Which columns the generated event CSV carries. Configured rather than
        # coded so the file can be lined up with whichever spreadsheet the team
        # reconciles against. Falls back to the module default, so an older
        # config.yaml still produces a usable export instead of an empty one.
        self.event_columns: List[Tuple[str, str]] = _event_columns(raw.get("exports") or {})

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

    @staticmethod
    def _resolve(path: str) -> str:
        """Relative paths are relative to the code, not to the shell's cwd."""
        return path if os.path.isabs(path) else os.path.join(BASE_DIR, path)

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
    def auth_enabled(self) -> bool:
        """Basic auth is on as soon as a password exists. No separate switch -
        one that could be left off while a password sits in .env is worse than
        no switch at all."""
        return bool(self.auth_password)

    @property
    def has_db_password(self) -> bool:
        return bool(self.database.get("password"))


def load_config(path: str = CONFIG_PATH) -> Config:
    _load_dotenv()
    with open(path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    return Config(raw)
