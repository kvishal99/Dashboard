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

# The script being run - last path component ending in a known extension.
SCRIPT_RE = re.compile(r"(/[^\s;|>]+\.(?:php|sh|py|pl))", re.I)

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

        script_match = SCRIPT_RE.search(command)
        redirect = None
        for m in REDIRECT_RE.finditer(command):
            target = m.group(1)
            if target not in ("/dev/null", "&1", "&2"):
                redirect = target
                break

        rows.append({
            "line_no": i,
            "schedule": schedule,
            "schedule_human": humanise(schedule),
            "command": command,
            "script": script_match.group(1) if script_match else None,
            "partner": guess_partner(command, known_partners),
            "log_file": redirect,
            "disabled": disabled,
        })
    return rows
