"""Is each partner's ingest job still working?

The signal is MAX(created) from jos_eventlist_events - when the partner last had
anything inserted - compared against how often its cron is supposed to run. A
partner whose cron says "Daily" but whose newest row is three weeks old has a
broken job, and that conclusion needs no access to the servers at all.

Frequency strings come from the Monday sheet and are messy free text
("Alternate weekly", "Monthly(Manual)", "Mannual (Provide by ramesh/sudhan)",
"wed-sat"), so parsing is deliberately forgiving and returns None rather than
guessing when a value carries no schedule.
"""
import re
from datetime import datetime
from typing import Any, Dict, Optional

# How many days between runs, by keyword. Checked in order - the first pattern
# that matches wins, so more specific phrases must come first.
FREQUENCY_DAYS = [
    (r"alternate\s*day", 2),
    (r"\bdaily\b|every\s*day", 1),
    (r"wed\s*-\s*sat|mon\s*-\s*fri", 7),      # named-day schedules run weekly
    (r"alternate\s*week|fortnight|every\s*2\s*week", 14),
    (r"\bweek", 7),
    (r"\bmonth", 30),
    (r"quarter", 90),
    (r"\byear", 365),
]

# Values that describe WHO does it, not WHEN - no schedule can be inferred.
NO_SCHEDULE = re.compile(
    r"manual|mannual|one\s*-?\s*time|onetime|new partner|need to add|provide", re.I
)

# A job is only flagged once it is this many times past its interval, so a
# weekly job running a day late is not reported as broken.
GRACE_MULTIPLIER = 2.0

# Notes in the sheet that mean a partner was switched off ON PURPOSE. Flagging
# these as broken is a false alarm - someone already decided to stop them.
RETIRED = re.compile(
    r"remove[d]?\s+(from|as per|the)|stop\s+this\s+partner|stopp?ed|"
    r"has stopped providing|partner removed|closed|delete all their events|"
    r"tournament is finished|not working|scrapping not allowed|feed not available",
    re.I,
)

# A partner with no cron schedule AND barely any events is not a broken job -
# it is a one-off import or a stray venue record. Below this many live events,
# an unscheduled partner is reported as dormant rather than stalled.
DORMANT_MAX_LIVE = 20


def expected_days(frequency: Optional[str]) -> Optional[int]:
    """Days expected between runs, or None if the text implies no schedule."""
    if not frequency:
        return None
    text = frequency.strip()
    if not text:
        return None
    # "weekly (manual)" still tells us weekly, so look for a period first and
    # only fall through to NO_SCHEDULE when nothing periodic is present.
    for pattern, days in FREQUENCY_DAYS:
        if re.search(pattern, text, re.I):
            return days
    if NO_SCHEDULE.search(text):
        return None
    return None


def _parse(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d"):
        try:
            return datetime.strptime(ts, fmt)
        except ValueError:
            continue
    return None


def assess(last_created: Optional[str], frequency: Optional[str],
           now: Optional[datetime] = None, note: Optional[str] = None,
           live_events: Optional[int] = None) -> Dict[str, Any]:
    """Classify one partner's job freshness.

    state is one of:
      ok        - inserted within its expected interval
      late      - overdue, but less than the grace window
      stalled   - well past due; the job is very likely broken
      retired   - the sheet says it was switched off deliberately
      dormant   - no schedule and hardly any events; not really a job
      unknown   - no schedule in the sheet, so nothing to compare against
      never     - nothing ever inserted
    """
    now = now or datetime.now()
    last = _parse(last_created)
    interval = expected_days(frequency)

    # Checked before staleness: a partner someone deliberately stopped is not a
    # fault, and reporting it as one trains people to ignore the tab.
    retired = bool(note and RETIRED.search(note))

    if last is None:
        return {
            "days_since": None, "expected_days": interval,
            "state": "retired" if retired else "never", "overdue_by": None,
            "retired": retired,
        }

    days_since = (now - last).total_seconds() / 86400.0

    if interval is None:
        # Nothing to compare against. A tiny unscheduled partner that has not
        # moved in a year is dormant - a one-off import or a stray record - not
        # a broken cron. Only flag the ones big enough to matter.
        if retired:
            state = "retired"
        elif days_since <= 365:
            state = "unknown"
        elif live_events is not None and live_events <= DORMANT_MAX_LIVE:
            state = "dormant"
        else:
            state = "stalled"
        return {
            "days_since": round(days_since, 1), "expected_days": None,
            "state": state, "overdue_by": None, "retired": retired,
        }

    if days_since <= interval:
        state = "ok"
    elif retired:
        state = "retired"
    elif days_since <= interval * GRACE_MULTIPLIER:
        state = "late"
    else:
        state = "stalled"

    return {
        "days_since": round(days_since, 1),
        "expected_days": interval,
        "state": state,
        "overdue_by": round(max(days_since - interval, 0), 1),
        "retired": retired,
    }


# Worst first, for sorting.
STATE_RANK = {
    "stalled": 0, "never": 1, "late": 2,
    "unknown": 3, "dormant": 4, "retired": 5, "ok": 6,
}

# The states the "Problems only" filter shows - things someone should act on.
PROBLEM_STATES = {"stalled", "late", "never"}
