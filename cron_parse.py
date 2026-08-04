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


# Directories that sit alongside the partner directories under
# com_events_venue/ but are shared machinery, not a partner. Without this the
# path-based guess reports "UnpublishDuplicates" and "alternateCron" as partners
# with 20-odd cron jobs each, and they then appear in every partner list in the
# dashboard.
NOT_A_PARTNER = {
    "unpublishduplicates", "alternatecron", "activeevents", "com_reports",
    "transfer_details", "migrate_artist_notice", "modified_title",
    "event_artist_association", "tribute_event_title", "venue_mapping",
    "weekly-event-cron.sh", "daily-event-cron.sh", "cron", "crons", "cronjob",
    "scripts", "logs", "test", "tmp", "bin", "lib", "includes", "common",
}

# A partner directory is a single path segment: letters, digits and mild
# punctuation. Anything with a space, a redirect or a script extension is a
# fragment of the command line that got captured, not a directory name.
_PLAUSIBLE_PARTNER = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{1,39}$")


def _partner_in_text(text: str, known: Optional[set]) -> Optional[str]:
    """The known partner named inside a filename, or None.

    Only ever matches against the known list - a wrapper script called
    `viagogo_weekly_event_cron.sh` names viagogo, but nothing here should
    invent a partner out of `weekly` or `cron`.

    Longest name first, so `eventim-uk` wins over `eventim` in
    `daily_event_cron_eventim_ticketcity_eventim-uk.sh`, and a 4-character
    floor keeps short names from matching inside unrelated words.
    """
    if not known:
        return None
    lowered = text.lower()
    for name in sorted(known, key=len, reverse=True):
        if len(name) >= 4 and name.lower() in lowered:
            return name
    return None


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

        # Not a sub-directory but a script sitting in the partner root -
        # `com_events_venue/viagogo_weekly_event_cron.sh`. These are wrapper
        # scripts that run one partner's job, and they name the partner in the
        # filename, so the name is taken from there rather than given up on.
        # Confirmed against the real crontabs: 16 lines are shaped this way.
        if candidate.lower().endswith((".php", ".sh", ".py", ".pl")) or " " in candidate:
            found = _partner_in_text(candidate, known)
            if found:
                return found
            continue

        # Shape check. The old version only rejected a candidate ENDING in .php
        # or .sh, so "daily-event-cron.sh >" - the tail of a line whose marker
        # directory had no sub-directory - sailed through and became a partner.
        if not _PLAUSIBLE_PARTNER.match(candidate):
            continue
        if candidate.lower() in NOT_A_PARTNER:
            continue

        if known is None:
            return candidate
        lowered = {k.lower(): k for k in known}
        if candidate.lower() in lowered:
            return lowered[candidate.lower()]
        # Directory names drift from partner names (sportsEvent vs
        # SportsEvents365, gicpig vs gigpig), so try a loose contains match.
        # Anchored at 4 characters: a 2-letter partner name is a substring of
        # half these paths, which is how "bw" would claim other people's jobs.
        for key, original in lowered.items():
            if len(key) >= 4 and (key in candidate.lower() or candidate.lower() in key):
                return original
        # Not in either list, but it IS a partner directory - fandango,
        # ticketsnow and reservix all have crons here while having no rows in
        # MySQL and no line in the status sheet. That combination is worth
        # seeing, so the directory name is kept rather than dropped.
        return candidate

    # Not under a partner root at all. A partner's work is not only ingest:
    # `/var/www/html/venuepilot_nodejs/venue.sh` is venuepilot's site watchdog
    # and lives nowhere near com_events_venue. Match a path segment that IS a
    # known partner, or starts with one followed by a separator, so the job
    # still reaches that partner's page.
    return _partner_in_path(command, known)


# A path segment that is a known partner, optionally with a suffix like
# `_nodejs` or `-cron`. Anchored to the whole segment so `stagerX` cannot match
# `stager`, and applied only to names of 4+ characters.
def _partner_in_path(command: str, known: Optional[set]) -> Optional[str]:
    if not known:
        return None
    segments = {s for s in re.split(r"[/\s]+", command) if s}
    lowered = {k.lower(): k for k in known if len(k) >= 4}
    for segment in segments:
        low = segment.lower()
        for name, original in lowered.items():
            if low == name or re.match(rf"^{re.escape(name)}[_-][a-z0-9]+$", low):
                return original
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


# ---------------------------------------------------------------------------
# What kind of job is this?
# ---------------------------------------------------------------------------

# Every cron line used to be presented as though it were a partner data
# insertion, which is wrong for most of them: of the 289 collected lines only a
# minority insert events at all. The rest watch processes, generate CSV feeds,
# unpublish duplicates or push images to a CDN - different work, different
# people, and a different reaction when one breaks.
#
# Categories are ordered: the first pattern that matches wins, most specific
# first. Matching runs against the SCRIPT PATH, not the whole command line -
# three quarters of these lines redirect into a `.csv` file, so classifying on
# the raw command would file almost everything under "CSV".
CATEGORIES = [
    ("health", "Website Health",
     "Watchdogs, process killers and connectivity probes - jobs that check "
     "something is alive rather than move data."),
    ("scraper", "Scrapers",
     "Jobs that fetch from a partner site or API: scrapers, crawlers and feed "
     "downloads."),
    ("import", "Import Jobs",
     "Jobs that insert or update event records in the database."),
    ("csv", "CSV & Reports",
     "Jobs that generate a file or a report - feed exports, counts, mailed "
     "reports."),
    ("maintenance", "Maintenance",
     "Housekeeping: unpublishing duplicates, deleting expired rows, image and "
     "search-index upkeep."),
    ("other", "Other", "Everything that does not fall into the categories above."),
]

CATEGORY_LABELS = {key: label for key, label, _ in CATEGORIES}

# First match wins, so these are ordered by how specific they are rather than by
# the display order in CATEGORIES. The ordering does real work: partner ingest
# lives under `com_events_venue/`, but so does `com_events_venue/
# UnpublishDuplicates/`, which is housekeeping - so the maintenance rule has to
# be tested before the directory-wide import rule.
_CATEGORY_RULES = [
    # Narrow on purpose. "ping" without word boundaries matched every
    # *_artist_Mapping* job, which is data work, not a health check.
    # `_nodejs/` and `_nextjs` are here, not under import, because every one of
    # these scripts was read on the servers and every one is a port check plus a
    # pm2 restart. `venuepilot_nodejs/venue.sh` greps for :3013 and restarts the
    # app - it sits in a partner-named directory and touches no data at all.
    ("health", re.compile(
        r"watchdog|kill_process|killprocess|testconnection|\bhealth\b|"
        r"heartbeat|uptime|\bmonitor|\bping\b|site_check|url_check|"
        r"check_site|restart_|diskspace|mailstatus|checkmail|"
        r"_nodejs/|_nextjs|nextjs\.sh|nodejs\.sh|webhook", re.I)),
    ("scraper", re.compile(
        r"scrap|crawl|/spider|fetch_feed|feed_download|selenium|puppeteer", re.I)),
    ("maintenance", re.compile(
        r"unpublishduplicates/|unpublish|duplicate|deceased|delete_|expired|"
        r"archive|cleanup|clearlog|cdnimages|image_upload|copyimages|"
        r"getartistimage|sphinx|migrate_|ucwords|update_city|visibility|"
        r"artist|mapping", re.I)),
    ("csv", re.compile(
        r"report|feed_generation|feed_generations|generate|export|"
        r"event_count|poicitycount|send_file|send_.*mail|sitemap", re.I)),
    # The bulk of what this dashboard tracks: partner ingest under
    # com_events_venue/ and eventPartner*/, plus the per-partner shells there
    # (daily-event-cron.sh, altsat.sh, *_nodejs/x.sh) that do the inserting.
    ("import", re.compile(
        r"com_events_venue/|eventpartner|insertevent|insert_event|/insert|"
        r"insert[a-z0-9_-]*\.php|import|event[_-]cron|eventcron|"
        r"alternate[_-]?cron|altsat|-event-cron|update_count|"
        r"updatecount|movie_cronjob|movieweekly|daily_movie|cronjob/", re.I)),
]


# What a script DOES, read from the script itself.
#
# Paths lie. `venuepilot_nodejs/venue.sh` sits in a partner-named directory and
# looks like an ingest job; it actually runs `netstat | grep :3013` and restarts
# the Next.js app if the port is dead - a website uptime watchdog with no
# database access at all. The same is true of eventseeker, cityseeker, cobbar,
# nearmy and rtr. Reading the file settles it; guessing from the path cannot.
#
# Order is by decisiveness, not by display order:
#   * a port check and a service restart mean a watchdog whatever else is there
#   * INSERT INTO means the job loads records, even though ingest scripts also
#     curl the partner's feed - so import is tested before scraper
#   * unpublish/DELETE is housekeeping, and is tested before the broad CSV rule
#     because these scripts usually mail a report about what they cleaned up
_CONTENT_RULES = [
    # Deliberately narrow. `http_code` and a bare `pkill` were in here and
    # matched any script that uses curl at all - a push-notification sender and
    # a proxy fetcher both came back as "website health". A watchdog is
    # specifically something that inspects whether a service is listening and
    # restarts it.
    ("health", re.compile(
        r"netstat\b|\bss\s+-[tulnp]|not listening|Service running fine|"
        r"pm2\s+(start|restart|resurrect)|systemctl\s+(restart|status)|"
        r"supervisorctl|service\s+\w+\s+restart|is not running", re.I)),
    ("import", re.compile(
        r"INSERT\s+INTO|REPLACE\s+INTO|insertEvent|insert_event|->\s*insert\s*\(", re.I)),
    ("maintenance", re.compile(
        r"unpublish|DELETE\s+FROM|TRUNCATE\s|duplicate", re.I)),
    ("scraper", re.compile(
        r"curl_init|CURLOPT_URL|file_get_contents\s*\(\s*[\"']?https?://|"
        r"Guzzle|simple_html_dom|wget\s+https?://|selenium|puppeteer", re.I)),
    ("csv", re.compile(
        r"fputcsv|PHPMailer|\bmail\s*\(|SendMail|fopen\s*\([^)]*\.csv", re.I)),
]


# The same signatures as a flat token list, for grepping the WHOLE file on the
# server rather than reading its first few KB back.
#
# Reading a fixed head was wrong and measurably so: `insertEvent.php` and
# `insert_events_ticketevolution.php` open with includes and configuration, so
# their `INSERT INTO` sits past any reasonable head, and both were classified
# from a stray `fopen(...log)` further up. Grepping costs one line of output per
# file instead of 2.5 KB, and scans all of it.
#
# ERE, single-quote free so it can be embedded in a quoted remote command.
SIGNATURE_TOKENS = (
    "netstat|not listening|service running fine|pm2 (start|restart)|"
    "systemctl (restart|status)|supervisorctl|"
    "insert into|replace into|insertevent|insert_event|"
    "unpublish|delete from|truncate table|"
    "curl_init|curlopt_url|file_get_contents|guzzle|simple_html_dom|"
    "fputcsv|phpmailer"
)

# token (lowercased, as grep returns it) -> category. Order of the CATEGORY
# priority below is what resolves a file that matches several, which most real
# ingest scripts do - they curl a feed AND insert what they find.
_TOKEN_CATEGORY = [
    ("health", ("netstat", "not listening", "service running fine",
                "pm2 start", "pm2 restart", "systemctl restart",
                "systemctl status", "supervisorctl")),
    ("import", ("insert into", "replace into", "insertevent", "insert_event")),
    ("maintenance", ("unpublish", "delete from", "truncate table")),
    ("scraper", ("curl_init", "curlopt_url", "file_get_contents", "guzzle",
                 "simple_html_dom")),
    ("csv", ("fputcsv", "phpmailer")),
]


def categorise_tokens(tokens) -> Optional[str]:
    """Category from the signature tokens grepped out of a script.

    A script that both fetches a feed and inserts what it finds is an import
    job, not a scraper - which is why import is resolved before scraper rather
    than by whichever token happened to appear first in the file.
    """
    found = {t.strip().lower() for t in tokens if t and t.strip()}
    if not found:
        return None
    for category, markers in _TOKEN_CATEGORY:
        if any(m in found for m in markers):
            return category
    return None


def categorise_content(body: str) -> Optional[str]:
    """The category implied by a script's own source, or None if it says nothing.

    None is a real answer - a two-line wrapper that calls another script tells
    us nothing, and inventing a category from it would be worse than falling
    back to the path.
    """
    if not body or not body.strip():
        return None
    for key, pattern in _CONTENT_RULES:
        if pattern.search(body):
            return key
    return None


def categorise(script: Optional[str], command: str, name: str = "") -> str:
    """Which category this crontab line belongs to.

    Deliberately classified on the script path rather than the full command:
    most of these lines end in `> something.csv`, so the redirect target would
    otherwise decide the answer for three quarters of them.
    """
    # The script path is the signal. Falling back to the command is only for
    # lines with no resolvable path at all, and even then the redirect is
    # stripped first so the same mistake cannot creep back in.
    subject = script or REDIRECT_RE.sub("", command)
    subject = f"{subject} {name}".strip()

    for key, pattern in _CATEGORY_RULES:
        if pattern.search(subject):
            return key
    return "other"


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
        name = job_name(target, command)
        rows.append({
            "line_no": i,
            "schedule": schedule,
            "schedule_human": humanise(schedule),
            "command": command,
            "script": target,
            "name": name,
            "partner": guess_partner(command, known_partners),
            "category": categorise(target, command, name),
            "log_file": redirect,
            "disabled": disabled,
        })
    return rows
