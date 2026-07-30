#!/usr/bin/env python3
"""Collect every crontab entry from the partner servers into the dashboard.

    ./venv/bin/python collect-crons.py              # every reachable host
    ./venv/bin/python collect-crons.py 3.94.49.56   # just one
    ./venv/bin/python collect-crons.py --dry-run    # print, don't store

The dashboard also does this on its own every few hours (see
`app.cron_interval_seconds` in config.yaml), so running this by hand is only
needed when you want the list refreshed immediately.

All the work lives in cron_collect.py, which the scheduler imports too.
"""
import sys

import cron_collect
from config import load_config
from store import Store


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    hosts = [a for a in sys.argv[1:] if not a.startswith("--")]

    config = load_config()
    store = Store(config.db_path)

    print(f"Collecting crontabs{' (dry run)' if dry_run else ''}\n")
    result = cron_collect.collect(
        config, store, hosts=hosts or None, dry_run=dry_run,
        log=lambda m: print(f"  {m}"),
    )

    print(f"\n{'would store' if dry_run else 'stored'} {result['total']} cron jobs "
          f"from {result['reachable']} of {len(result['hosts'])} hosts")
    unreachable = [h for h in result["hosts"] if not h["ok"]]
    if unreachable:
        print("\nskipped:")
        for h in unreachable:
            print(f"  {h['host']}: {h['error']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
