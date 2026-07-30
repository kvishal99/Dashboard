"""Parse crontab lines into something a dashboard can display.

Handles the shapes that actually appear in these crontabs:

    1 0 * * 5 /usr/bin/php /home/.../ticketevolution/insert_events...php > out.csv
    00 02,14 * * * /usr/bin/php /home/.../mark_cancel_TM.php > noschedule.log
    @daily /path/script.sh
    MAILTO=""                      <- env assignment, not a job
    #00 11 * * 1 /usr/bin/php ...   <- disabled job
"""
import re
from typing import Any, Dict, List, Optional

DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]

SPECIALS = {
    "@reboot": "at boot",
    "@yearly": "yearly", "@annually": "yearly",
    "@monthly": "monthly", "@weekly": "weekly",
    "@daily": "daily", "@midnight": "daily",
    "@hourly": "hourly",
}

# Env assignments at the top of a crontab (MAILTO=, PATH=, SHELL=).
ENV_LINE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\s*=")

# The script being run - a path ending in a known extension.
SCRIPT_RE = re.compile(r"(/[^\s;|>]+\.(?:php|sh|py|pl|rb|js))", re.I)

# Any absolute path, for commands whose target has no extension at all
# (e.g. /var/www/html/xmlgen/wcities/events/partner_eventcron).
ANY_PATH_RE = re.compile(r"(/[A-Za-z0-9._@/-]{4,})")

# Things that appear as an absolute path but are not the job: interpreters,
# wrappers, and lock/temp files.
# Matches ANY .../bin/<interpreter>, not just /usr/bin - a virtualenv's
# .../venv/bin/python is just as much "not the job" as /usr/bin/python.
NOT_THE_JOB = re.compile(
    r"(?:^|/)bin/"
    r"(?:php\d?|bash|sh|zsh|python\d?(?:\.\d+)?|perl|node|env|nice|"
    r"flock|timeout|nohup|ionice|xargs|stat|date|find)$"
    r"|^/tmp/|^/dev/|\.lock$",
    re.I,
)

# Output redirect: "> file", ">> file". Ignores 2>&1 and /dev/null.
REDIRECT_RE = re.compile(r"(?<!\d)>>?\s*([^\s;|&]+)")


def _field(value: str, names: Optional[List[str]] = None) -> str:
    if value == "*":
        return "every"
    if value.startswith("*/"):
        return f"every {value[2:]}"
    if names:
        parts = []
        for chunk in value.split(","):
            try:
                parts.append(names[int(chunk) % len(names)])
            except ValueError:
                parts.append(chunk)
        return ",".join(parts)
    return value


def humanise(schedule: str) -> str:
    """Best-effort plain English. Falls back to the raw spec."""
    if schedule in SPECIALS:
        return SPECIALS[schedule]
    parts = schedule.split()
    if len(parts) != 5:
        return schedule
    minute, hour, dom, month, dow = parts

    def at(h: str, m: str) -> str:
        """Render the time-of-day part, handling multi-hour lists like 02,14."""
        mm = m.zfill(2) if m.isdigit() else m
        if h.startswith("*/"):
            return f"every {h[2:]}h at :{mm}"
        if "," in h:
            return " and ".join(f"{x.zfill(2)}:{mm}" for x in h.split(","))
        return f"{h.zfill(2) if h.isdigit() else h}:{mm}"

    if dom == "*" and month == "*" and dow == "*":
        if minute.startswith("*/") and hour == "*":
            return f"every {minute[2:]} minutes"
        if hour == "*":
            return "every minute" if minute == "*" else f"hourly at :{minute.zfill(2)}"
        return f"daily at {at(hour, minute)}"
    if dow != "*" and dom == "*":
        return f"{_field(dow, DAYS)} at {at(hour, minute)}"
    if dom != "*":
        return f"day {dom} of month at {at(hour, minute)}"
    return schedule


def guess_partner(command: str, known: Optional[set] = None) -> Optional[str]:
    """Pull a partner name out of the path.

    Paths look like .../eventPartner_174/ticketevolution/insert_events.php or
    .../com_events_venue/dice.fm/insert_events.php, so the directory after the
    partner root is the best clue. Matched against the known partner list when
    one is supplied, so a wrong guess is not invented.
    """
    for marker in ("com_events_venue/", "eventPartner_174/", "eventPartner/"):
        idx = command.find(marker)
        if idx == -1:
            continue
        rest = command[idx + len(marker):]
        candidate = rest.split("/")[0].strip()
        if not candidate or candidate.endswith((".php", ".sh")):
            continue
        if known is None:
            return candidate
        lowered = {k.lower(): k for k in known}
        if candidate.lower() in lowered:
            return lowered[candidate.lower()]
        # Directory names drift from partner names (sportsEvent vs
        # SportsEvents365, gicpig vs gigpig), so try a loose contains match.
        for key, original in lowered.items():
            if key and (key in candidate.lower() or candidate.lower() in key):
                return original
        return candidate
    return None


def primary_target(command: str, redirect: Optional[str] = None) -> Optional[str]:
    """The path that identifies what this job actually runs.

    Prefers a path with a script extension. Falls back to the first absolute
    path that isn't an interpreter, wrapper, lock file or the output redirect -
    which is how extensionless jobs like `.../partner_eventcron` get named.
    """
    match = SCRIPT_RE.search(command)
    if match:
        return match.group(1)

    candidates = [
        c.rstrip("/") for c in ANY_PATH_RE.findall(command)
        if not NOT_THE_JOB.search(c) and not (redirect and c == redirect)
    ]
    # "cd /some/dir && real/job" - the first path is the working directory, not
    # the thing being run, so skip it when another candidate follows.
    if command.lstrip().startswith("cd ") and len(candidates) > 1:
        candidates = candidates[1:]
    return candidates[0] if candidates else None


def job_name(target: Optional[str], command: str) -> str:
    """A short label a human can recognise in a list of 400 jobs.

    Uses the last two path components, because the directory usually carries
    the meaning ("marriott_mvc/mvc.sh" beats a bare "mvc.sh", and there are
    two different "yelp.sh" entries on one server).
    """
    if target:
        parts = [p for p in target.split("/") if p]
        if len(parts) >= 2:
            return "/".join(parts[-2:])
        if parts:
            return parts[-1]
    # No path at all - fall back to the first meaningful word of the command.
    for token in command.split():
        if token in ("cd", "if", "[", "&&", "||") or token.startswith("-"):
            continue
        return token[:40]
    return "(unnamed)"


def parse_crontab(text: str, known_partners: Optional[set] = None) -> List[Dict[str, Any]]:
    """Turn `crontab -l` output into structured rows."""
    rows: List[Dict[str, Any]] = []
    for i, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue

        disabled = False
        if line.startswith("#"):
            stripped = line.lstrip("#").strip()
            # Only treat it as a disabled job if it still looks like one;
            # ordinary comments are skipped.
            if not (re.match(r"^[\d*@]", stripped) and len(stripped.split()) > 1):
                continue
            line, disabled = stripped, True

        if ENV_LINE.match(line):
            continue

        if line.startswith("@"):
            parts = line.split(None, 1)
            if len(parts) < 2:
                continue
            schedule, command = parts[0], parts[1]
        else:
            parts = line.split(None, 5)
            if len(parts) < 6:
                continue
            schedule, command = " ".join(parts[:5]), parts[5]

        redirect = None
        for m in REDIRECT_RE.finditer(command):
            target = m.group(1)
            if target not in ("/dev/null", "&1", "&2"):
                redirect = target
                break

        target = primary_target(command, redirect)
        rows.append({
            "line_no": i,
            "schedule": schedule,
            "schedule_human": humanise(schedule),
            "command": command,
            "script": target,
            "name": job_name(target, command),
            "partner": guess_partner(command, known_partners),
            "log_file": redirect,
            "disabled": disabled,
        })
    return rows
