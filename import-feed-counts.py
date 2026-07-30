#!/usr/bin/env python3
"""Fill the "In partner feed" column from the Monday status sheet.

    ./venv/bin/python import-feed-counts.py                    # default CSV path
    ./venv/bin/python import-feed-counts.py path/to/sheet.csv
    ./venv/bin/python import-feed-counts.py --dry-run

The partner-side total is not stored in MySQL, but the sheet records both
halves of it:

    partner side  =  "Partner Total Count"  +  "Not inserted count"
                     (what we took)            (what we rejected)

That's a point-in-time snapshot, not live - both numbers come from whenever the
sheet was last updated, so they're consistent with each other but will drift
from the database over time. Rows are tagged source="sheet" so you can tell
them apart from live figures pushed by an ingest script, and any later
`script` report for a partner simply supersedes this one.
"""
import csv
import json
import os
import re
import sys
import urllib.error
import urllib.request

from config import BASE_DIR, load_config

DEFAULT_CSV = "/home/vishal/Downloads/Monday Partner Status - Final.csv"
ENDPOINT = os.environ.get("OPS_DASHBOARD", "http://127.0.0.1:8000")


def as_int(raw: str):
    """Parse a count cell. Handles '1,234' and multi-line text like
    'EB catalog - 40179 / EB catalog - 7' by summing the numbers it finds."""
    if not raw:
        return None
    cleaned = raw.replace(",", "").strip()
    if re.fullmatch(r"\d+", cleaned):
        return int(cleaned)
    nums = [int(n) for n in re.findall(r"\b\d{2,}\b", cleaned)]
    return sum(nums) if nums else None


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry_run = "--dry-run" in sys.argv
    path = args[0] if args else DEFAULT_CSV

    if not os.path.isfile(path):
        print(f"CSV not found: {path}")
        return 1

    config = load_config()
    secret = config.agent_secret

    with open(path, encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))

    def g(r, k):
        return (r.get(k) or "").strip()

    # Keep the row with the largest total when a partner appears twice.
    best = {}
    for r in rows:
        name = g(r, "Partner")
        if not name:
            continue
        total = as_int(g(r, "Partner Total Count")) or 0
        if name not in best or total > (as_int(g(best[name], "Partner Total Count")) or 0):
            best[name] = r

    sent = skipped = failed = 0
    print(f"{'partner':<24}{'our DB':>10}{'not ins':>10}{'partner side':>14}")

    for name, r in sorted(best.items(), key=lambda kv: kv[0].lower()):
        if config.is_excluded(name):
            continue
        total = as_int(g(r, "Partner Total Count"))
        not_ins = as_int(g(r, "Not inserted count"))
        if total is None or not_ins is None:
            skipped += 1
            continue

        feed_count = total + not_ins
        print(f"{name:<24}{total:>10,}{not_ins:>10,}{feed_count:>14,}")
        if dry_run:
            sent += 1
            continue

        payload = json.dumps({
            "partner": name,
            "feed_count": feed_count,
            "inserted": total,
            "source": "sheet",
            "note": f"from {os.path.basename(path)}",
        }).encode()
        req = urllib.request.Request(
            f"{ENDPOINT}/api/partners/feed-count",
            data=payload,
            headers={"Content-Type": "application/json", "x-agent-secret": secret},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10):
                sent += 1
        except urllib.error.HTTPError as exc:
            failed += 1
            hint = " - OPS_AGENT_SECRET mismatch?" if exc.code == 401 else ""
            print(f"   FAILED {name}: HTTP {exc.code}{hint}")
        except Exception as exc:
            failed += 1
            print(f"   FAILED {name}: {exc}")

    print()
    verb = "would import" if dry_run else "imported"
    print(f"{verb} {sent} partners · skipped {skipped} (no 'Not inserted count') · failed {failed}")
    if not dry_run and sent:
        print("\nThese are sheet snapshots. An ingest script reporting live numbers")
        print("via report_to_dashboard.php will supersede them automatically.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
