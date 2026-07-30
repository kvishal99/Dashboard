#!/usr/bin/env python3
"""Pre-flight check: can the dashboard reach MySQL, and do the queries work?

    ./venv/bin/python check_db.py            # connect + run both queries for 3 partners
    ./venv/bin/python check_db.py fever      # run them for one named partner

Read-only. It issues nothing but SELECTs.
"""
import sys
import time

import mysql
from config import load_config

# The known-divergent local copy. Used only to print a warning, never to gate.
LAPTOP_HOSTNAME = "vishal-konale"


def main() -> int:
    config = load_config()
    db = config.database

    print(f"host      {db['host']}:{db['port']}")
    print(f"database  {db['database']}")
    print(f"user      {db['user']}")
    print(f"password  {'set' if config.has_db_password else 'NOT SET'}")
    print()

    if not config.has_db_password:
        print("No password configured. Put OPS_DB_PASSWORD in .env (see .env.example)")
        print("or export it before running. Continuing - the server may not need one.")
        print()

    print("1. Connecting...")
    ping = mysql.ping(db)
    if not ping["ok"]:
        print(f"   FAILED: {ping['error']}")
        print("\n   A '(2003) Can't connect ... timed out' is a TCP timeout - it")
        print("   happens before any password is sent, so it never means the")
        print("   credential is wrong. It means the host/port is unreachable.")
        return 1
    print(f"   OK - MySQL {ping['version']}")

    # Which server actually answered. The laptop copy and the production master
    # both listen on 127.0.0.1:3306 and both have an `admin` schema, so this is
    # the only reliable way to tell whose numbers you are looking at.
    print(f"\n   >>> answered by: {ping['hostname']} (server_uuid {ping['server_uuid']})")
    if ping["hostname"] == LAPTOP_HOSTNAME:
        print("   >>> This is the LOCAL laptop copy, which disagrees with the master")
        print("   >>> (active: 3,941/898 here vs 3,155/1,764 on the master).")
        print("   >>> For real numbers run:  ./run-with-tunnel.sh")
    else:
        print("   >>> This is NOT the laptop copy - numbers should match the master.")

    # Self-test of the read-only guard. assert_read_only() only inspects the
    # STRING - it opens no connection and executes nothing - so none of these
    # samples ever reach MySQL. They are rejected in Python and discarded.
    print("\n2. Self-testing the read-only guard (nothing is sent to MySQL)...")
    samples = [
        ("a write statement", "DELETE FROM some_table"),
        ("a schema change", "ALTER TABLE some_table ADD COLUMN x INT"),
        ("two stacked statements", "SELECT 1; SELECT 2"),
    ]
    for description, sample in samples:
        try:
            mysql.assert_read_only(sample)
            print(f"   PROBLEM: guard allowed {description}")
            return 1
        except mysql.UnsafeQuery as exc:
            print(f"   blocked: {description} ({exc})")

    if not sys.argv[1:]:
        # No partner named: exercise the query the scheduler actually runs.
        print("\n3. Running the grouped scan (the query the scheduler uses)...")
        import counts as counts_mod

        sweep = counts_mod.collect_all(db, config.queries)
        if not sweep.ok:
            print(f"   FAILED: {sweep.error}")
            return 1
        live = {
            n: r for n, r in sweep.partners.items()
            if not config.is_excluded(n) and (r.db_future or 0) >= config.min_live_events
        }
        print(f"   OK - {len(sweep.partners)} partners in the table, "
              f"{len(live)} shown after filters, in {sweep.duration_ms / 1000:.1f}s")
        print(f"\n   {'partner':<22}{'total':>12}{'live':>10}")
        for name, r in sorted(live.items(), key=lambda kv: -(kv[1].db_future or 0))[:8]:
            print(f"   {name:<22}{r.feed_total:>12,}{r.db_future:>10,}")
        print("\nAll good. Start the dashboard with:  ./venv/bin/python dashboard.py")
        return 0

    partners = sys.argv[1:]
    print(f"\n3. Running the single-partner counts for {len(partners)} partner(s)...")
    print(f"   {'partner':<22}{'total inserted':>16}{'live':>14}{'time':>9}")

    failures = 0
    for name in partners:
        started = time.perf_counter()
        try:
            feed = mysql.scalar(db, config.queries["feed_total"], (name,))
            live = mysql.scalar(db, config.queries["db_future"], (name,))
            secs = time.perf_counter() - started
            print(f"   {name:<22}{feed:>16,}{live:>14,}{secs:>8.1f}s")
        except Exception as exc:
            failures += 1
            print(f"   {name:<22}  FAILED: {type(exc).__name__}: {exc}")

    if failures:
        print(f"\n{failures} partner(s) failed. If the error mentions an unknown")
        print("column, fix the `queries:` block in config.yaml.")
        return 1

    print("\nAll good. Start the dashboard with:  ./venv/bin/python dashboard.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
