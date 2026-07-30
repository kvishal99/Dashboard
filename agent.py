#!/usr/bin/env python3
"""PM2 reporter - runs on each server, posts process stats to the dashboard.

    SERVER_ID=prod-01 DASHBOARD_URL=http://127.0.0.1:8777/api/pm2/report \
    AGENT_SECRET=... python3 agent.py

Everything is configured by environment variable so the same file can be copied
to any server unchanged. Standard library only - no pip install on the target.
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

CONFIG = {
    # Name shown on the dashboard. Defaults to the machine's hostname.
    "server_id": os.environ.get("SERVER_ID") or os.uname().nodename,
    # Where to POST. When the dashboard runs on a laptop behind NAT this is a
    # port on *this* machine that an SSH reverse tunnel forwards back to it.
    "dashboard_url": os.environ.get(
        "DASHBOARD_URL", "http://127.0.0.1:8777/api/pm2/report"
    ),
    "secret": os.environ.get("AGENT_SECRET", "change-this-to-a-secure-token"),
    "interval_seconds": int(os.environ.get("INTERVAL_SECONDS", "5")),
    "pm2_bin": os.environ.get("PM2_BIN", "pm2"),
}


def get_pm2_processes():
    """Query the local PM2 daemon for process stats."""
    try:
        # stdout=PIPE + universal_newlines rather than capture_output/text:
        # those are Python 3.7+, and the target servers run 3.6.
        result = subprocess.run(
            [CONFIG["pm2_bin"], "jlist"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, check=True, timeout=30,
        )
        raw_data = json.loads(result.stdout)
    except FileNotFoundError:
        print(f"pm2 not found at {CONFIG['pm2_bin']!r} - set PM2_BIN", file=sys.stderr)
        return []
    except Exception as exc:
        print(f"Error reading PM2 status: {exc}", file=sys.stderr)
        return []

    processes = []
    for proc in raw_data:
        pm2_env = proc.get("pm2_env", {})
        monit = proc.get("monit", {})
        mem_bytes = monit.get("memory", 0)
        processes.append({
            "id": proc.get("pm_id", -1),
            "name": proc.get("name", "unknown"),
            "status": pm2_env.get("status", "unknown"),
            "cpu": monit.get("cpu", 0),
            "memory": round(mem_bytes / (1024 * 1024), 1) if mem_bytes else 0,
            "restarts": pm2_env.get("restart_time", 0),
            "uptime": pm2_env.get("pm_uptime", 0),
        })
    return processes


def send_report():
    processes = get_pm2_processes()
    payload = json.dumps({
        "server_id": CONFIG["server_id"],
        "processes": processes,
    }).encode("utf-8")

    req = urllib.request.Request(
        CONFIG["dashboard_url"],
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-agent-secret": CONFIG["secret"],
        },
        method="POST",
    )
    stamp = time.strftime("%H:%M:%S")
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            if response.status == 200:
                print(f"[{stamp}] sent {len(processes)} process(es)")
            else:
                print(f"[{stamp}] unexpected status {response.status}")
    except urllib.error.HTTPError as exc:
        # 401 here means AGENT_SECRET doesn't match app.agent_secret on the
        # dashboard - worth calling out, it looks like a network error otherwise.
        detail = " (secret mismatch?)" if exc.code == 401 else ""
        print(f"[{stamp}] HTTP {exc.code}{detail}", file=sys.stderr)
    except Exception as exc:
        print(f"[{stamp}] dashboard unreachable: {exc}", file=sys.stderr)


if __name__ == "__main__":
    print(f"PM2 agent starting: server_id={CONFIG['server_id']!r} "
          f"-> {CONFIG['dashboard_url']} every {CONFIG['interval_seconds']}s",
          flush=True)
    while True:
        send_report()
        sys.stdout.flush()
        time.sleep(CONFIG["interval_seconds"])
