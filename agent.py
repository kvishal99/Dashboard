#!/usr/bin/env python3
"""Server reporter - runs on each server, posts what it sees to the dashboard.

    SERVER_ID=prod-01 DASHBOARD_URL=https://monitor.wcities.com/api/pm2/report \
    AGENT_SECRET=... python3 agent.py

Reports two things, on two different clocks:

  PM2 processes - every INTERVAL_SECONDS (5s), because that is a live view
  crontab       - every CRON_INTERVAL_SECONDS (6h), because crontabs barely move

The crontab half exists so the dashboard never has to SSH anywhere. The server
reads its own `crontab -l` and stats its own log files - both of which it can do
without credentials - and pushes the result out. Nothing needs an inbound login,
and no SSH password has to be stored anywhere.

Everything is configured by environment variable so the same file can be copied
to any server unchanged. Standard library only - no pip install on the target.
"""
import base64
import gzip
import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


def _cron_url(pm2_url):
    """The cron endpoint that pairs with the configured PM2 one."""
    if pm2_url.endswith("/api/pm2/report"):
        return pm2_url[: -len("/api/pm2/report")] + "/api/cron/report"
    return pm2_url.rstrip("/") + "/api/cron/report"


def _base_url(pm2_url):
    """The dashboard root the other endpoints hang off."""
    if pm2_url.endswith("/api/pm2/report"):
        return pm2_url[: -len("/api/pm2/report")]
    return pm2_url.rstrip("/")


_DASHBOARD_URL = os.environ.get(
    "DASHBOARD_URL", "http://127.0.0.1:8777/api/pm2/report"
)

CONFIG = {
    # Name shown on the dashboard. Defaults to the machine's hostname.
    "server_id": os.environ.get("SERVER_ID") or os.uname().nodename,
    # How cron rows are keyed. Must be the same string the partner sheet uses
    # for this box (its IP), or the Jobs tab cannot tell that a partner's cron
    # lives here. deploy-agent.sh sets it to the host it deployed to.
    "server_ip": os.environ.get("SERVER_IP") or "",
    # Where to POST. When the dashboard runs on a laptop behind NAT this is a
    # port on *this* machine that an SSH reverse tunnel forwards back to it.
    "dashboard_url": _DASHBOARD_URL,
    "cron_url": os.environ.get("CRON_URL") or _cron_url(_DASHBOARD_URL),
    "secret": os.environ.get("AGENT_SECRET", "change-this-to-a-secure-token"),
    "interval_seconds": int(os.environ.get("INTERVAL_SECONDS", "5")),
    "cron_interval_seconds": int(os.environ.get("CRON_INTERVAL_SECONDS", "21600")),
    "pm2_bin": os.environ.get("PM2_BIN", "pm2"),
    # Where to ask for files the dashboard wants but cannot come and get.
    "files_url": os.environ.get("FILES_URL") or (_base_url(_DASHBOARD_URL) + "/api/agent/files"),
    # How much of a file to send per request, before compression. 4 MB keeps
    # each POST small enough to retry cheaply while still moving a 400 MB log
    # in a manageable number of round trips.
    "chunk_bytes": int(os.environ.get("CHUNK_BYTES", str(4 * 1024 * 1024))),
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


# Output redirect: "> file", ">> file". Kept in step with cron_parse.py on the
# dashboard, which does the real parsing - here we only need the paths to stat.
REDIRECT_RE = re.compile(r"(?<!\d)>>?\s*([^\s;|&]+)")


def get_crontab():
    """This user's crontab, raw.

    `crontab -l` exits non-zero when there is no crontab at all, which is not an
    error worth reporting - it just means no jobs. Returns None only when the
    command could not be run, so a real failure is distinguishable from empty.
    """
    try:
        result = subprocess.run(
            ["crontab", "-l"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, timeout=30,
        )
    except FileNotFoundError:
        print("crontab command not found", file=sys.stderr)
        return None
    except Exception as exc:
        print(f"Error reading crontab: {exc}", file=sys.stderr)
        return None
    if result.returncode != 0 and not result.stdout.strip():
        return ""       # no crontab for this user
    return result.stdout


def stat_logs(crontab_text):
    """mtime and size of every redirect target named in the crontab.

    This is the 'did it actually run' signal. Done locally with os.stat, which
    is why no remote access is needed for it any more.
    """
    logs = {}
    for line in crontab_text.splitlines():
        for match in REDIRECT_RE.finditer(line):
            path = match.group(1)
            if path in ("/dev/null", "&1", "&2") or path in logs:
                continue
            try:
                st = os.stat(path)
                logs[path] = {"mtime": st.st_mtime, "size": st.st_size}
            except Exception:
                pass    # not written yet, or not ours to read - both fine
    return logs


def post(url, payload, label):
    """POST one JSON body. Returns True on success; never raises."""
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-agent-secret": CONFIG["secret"],
        },
        method="POST",
    )
    stamp = time.strftime("%H:%M:%S")
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            if response.status == 200:
                print(f"[{stamp}] {label}")
                return True
            print(f"[{stamp}] unexpected status {response.status}")
    except urllib.error.HTTPError as exc:
        # 401 here means AGENT_SECRET doesn't match app.agent_secret on the
        # dashboard - worth calling out, it looks like a network error otherwise.
        detail = " (secret mismatch?)" if exc.code == 401 else ""
        print(f"[{stamp}] HTTP {exc.code}{detail}", file=sys.stderr)
    except Exception as exc:
        print(f"[{stamp}] dashboard unreachable: {exc}", file=sys.stderr)
    return False


def get_json(url):
    """GET one JSON body. Returns the parsed object, or None."""
    req = urllib.request.Request(
        url, headers={"x-agent-secret": CONFIG["secret"]})
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception:
        return None


def upload_file(request_id, path):
    """Send one file to the dashboard, in compressed chunks.

    This is the other half of "the servers push, the dashboard never logs in":
    the dashboard has no route into most of these boxes, so a file it wants has
    to leave from here.

    Read and sent a chunk at a time so a 400 MB log costs this agent a few MB
    of memory, and gzipped because these are run logs and SQL dumps that
    compress to a small fraction of their size.
    """
    seq = 0
    sent = 0
    try:
        total = os.path.getsize(path)
    except OSError as exc:
        post("%s/%d" % (CONFIG["files_url"], request_id),
             {"seq": 0, "error": str(exc)}, "reported unreadable file")
        return False

    try:
        with open(path, "rb") as fh:
            while True:
                block = fh.read(CONFIG["chunk_bytes"])
                if not block:
                    break
                buf = io.BytesIO()
                # mtime=0 so the same file produces the same bytes every time.
                with gzip.GzipFile(fileobj=buf, mode="wb",
                                   compresslevel=1, mtime=0) as gz:
                    gz.write(block)
                payload = {
                    "seq": seq,
                    "data": base64.b64encode(buf.getvalue()).decode("ascii"),
                    "done": False,
                    "total_bytes": total,
                }
                if not post("%s/%d" % (CONFIG["files_url"], request_id),
                            payload, "sent chunk %d" % seq):
                    return False
                sent += len(block)
                seq += 1
        # An empty final chunk closes it, so a zero-byte file still completes.
        return post("%s/%d" % (CONFIG["files_url"], request_id),
                    {"seq": seq, "data": "", "done": True, "total_bytes": total},
                    "uploaded %s (%d bytes)" % (os.path.basename(path), sent))
    except Exception as exc:
        post("%s/%d" % (CONFIG["files_url"], request_id),
             {"seq": seq, "error": str(exc)}, "reported failed upload")
        return False


def serve_file_requests():
    """Upload anything the dashboard has asked this server for."""
    server = CONFIG["server_ip"] or CONFIG["server_id"]
    result = get_json("%s?server=%s" % (
        CONFIG["files_url"], urllib.parse.quote(server, safe="")))
    if not result:
        return
    for request in result.get("requests", []):
        upload_file(request["id"], request["path"])


def send_report():
    processes = get_pm2_processes()
    post(CONFIG["dashboard_url"],
         {"server_id": CONFIG["server_id"], "processes": processes},
         "sent %d process(es)" % len(processes))


def send_cron_report():
    """Push this server's crontab. Returns True if it was accepted."""
    text = get_crontab()
    if text is None:
        return False
    logs = stat_logs(text)
    return post(
        CONFIG["cron_url"],
        {
            "server_id": CONFIG["server_id"],
            "server": CONFIG["server_ip"] or CONFIG["server_id"],
            "hostname": os.uname().nodename,
            "crontab": text,
            "logs": logs,
        },
        "sent crontab (%d lines, %d log file(s))" % (
            len(text.splitlines()), len(logs)),
    )


if __name__ == "__main__":
    print(f"Agent starting: server_id={CONFIG['server_id']!r} "
          f"-> {CONFIG['dashboard_url']} every {CONFIG['interval_seconds']}s; "
          f"crontab -> {CONFIG['cron_url']} every "
          f"{CONFIG['cron_interval_seconds']}s",
          flush=True)
    last_cron = 0.0
    while True:
        send_report()
        # Retried on the normal PM2 tick until one succeeds, so a dashboard that
        # was down at startup doesn't cost six hours of missing cron data.
        if time.time() - last_cron >= CONFIG["cron_interval_seconds"]:
            if send_cron_report():
                last_cron = time.time()
        # Files the dashboard has asked for. Checked on the normal tick, so a
        # request is picked up within a few seconds of being made.
        serve_file_requests()
        sys.stdout.flush()
        time.sleep(CONFIG["interval_seconds"])
