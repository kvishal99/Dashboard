"""Crontab collection over SSH - the library half.

The CLI wrapper lives in collect-crons.py and the dashboard scheduler imports
this module, so the same code path serves both the manual run and the periodic
background one.

Hosts come from SSH_HOST plus AGENT_HOSTS in .env, with per-host credentials
(SSH_PASSWORD_<host with dots turned into underscores>). A host that cannot be
reached is recorded as skipped and never blocks the others.

For each job the redirect target is also stat-ed, giving the last time that
cron actually wrote output - the closest thing to "did it run" without root
access to the system cron log.
"""
import os
import re
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple

import cron_parse
from config import BASE_DIR, load_config
from store import Store

SSH_BASE = [
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "ConnectTimeout=15",
    "-o", "BatchMode=no",
]


def env_from_dotenv() -> Dict[str, str]:
    """Read .env directly - values may contain characters a shell would mangle."""
    from config import ENV_PATH

    path = ENV_PATH
    out: Dict[str, str] = {}
    if not os.path.isfile(path):
        return out
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if not line or line.lstrip().startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def host_key(host: str) -> str:
    return re.sub(r"[.\-]", "_", host)


def creds(env: Dict[str, str], host: str) -> Tuple[str, Optional[str], Optional[str]]:
    k = host_key(host)
    user = env.get(f"SSH_USER_{k}") or env.get("SSH_USER") or "fcampbell"
    password = env.get(f"SSH_PASSWORD_{k}")
    key = env.get(f"SSH_KEY_{k}") or env.get("SSH_KEY")
    # The generic SSH_PASSWORD belongs to SSH_HOST only - reusing it elsewhere
    # would fire a wrong password at every other box.
    if not password and host == env.get("SSH_HOST"):
        password = env.get("SSH_PASSWORD")
    return user, password or None, key or None


def ssh_run(host: str, user: str, password: Optional[str], key: Optional[str],
            remote_cmd: str, timeout: int = 60) -> Tuple[bool, str]:
    """Run a command over SSH. Password goes via SSH_ASKPASS, never argv."""
    args = ["ssh", *SSH_BASE]
    env = dict(os.environ)
    askpass_path = None

    if key and os.path.isfile(key):
        args += ["-i", key, "-o", "BatchMode=yes"]
    elif password:
        fd, askpass_path = tempfile.mkstemp(prefix="ops-askpass-", suffix=".sh")
        with os.fdopen(fd, "w") as fh:
            fh.write('#!/usr/bin/env bash\nprintf %s "$OPS_SSH_PASSWORD"\n')
        os.chmod(askpass_path, 0o700)
        env.update({
            "OPS_SSH_PASSWORD": password,
            "SSH_ASKPASS": askpass_path,
            "SSH_ASKPASS_REQUIRE": "force",
            "DISPLAY": env.get("DISPLAY", ":0"),
        })
        args += ["-o", "PreferredAuthentications=password",
                 "-o", "PubkeyAuthentication=no",
                 "-o", "NumberOfPasswordPrompts=1"]
    else:
        return False, "no password or key configured"

    args += [f"{user}@{host}", remote_cmd]
    try:
        proc = subprocess.run(
            args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, timeout=timeout, env=env,
        )
        if proc.returncode != 0 and not proc.stdout.strip():
            return False, (proc.stderr or "").strip().splitlines()[-1:] and \
                          (proc.stderr or "").strip().splitlines()[-1] or "ssh failed"
        return True, proc.stdout
    except subprocess.TimeoutExpired:
        return False, f"timed out after {timeout}s"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        if askpass_path:
            os.unlink(askpass_path)


def stat_logs(host: str, user: str, password: Optional[str], key: Optional[str],
              paths: List[str]) -> Dict[str, Tuple[float, int]]:
    """mtime and size for each redirect target, in one round trip."""
    if not paths:
        return {}
    quoted = " ".join(f"'{p}'" for p in paths if "'" not in p)
    if not quoted:
        return {}
    ok, out = ssh_run(host, user, password, key,
                      f"stat -c '%n|%Y|%s' {quoted} 2>/dev/null || true", timeout=45)
    result: Dict[str, Tuple[float, int]] = {}
    if not ok:
        return result
    for line in out.splitlines():
        bits = line.strip().split("|")
        if len(bits) == 3:
            try:
                result[bits[0]] = (float(bits[1]), int(bits[2]))
            except ValueError:
                pass
    return result



def collect(config, store, hosts=None, dry_run=False, log=print) -> Dict[str, Any]:
    """Collect crontabs from every configured host.

    Returns a per-host summary. Never raises for a single unreachable host - it
    is recorded as skipped so one bad box cannot stop the rest.
    """
    env = env_from_dotenv()
    if not hosts:
        hosts = []
        if env.get("SSH_HOST"):
            hosts.append(env["SSH_HOST"])
        for h in re.split(r"[,\s]+", env.get("AGENT_HOSTS", "")):
            if h and h not in hosts:
                hosts.append(h)

    known = set(config.partner_meta.keys())
    results, total = [], 0

    for host in hosts:
        user, password, key = creds(env, host)
        ok, out = ssh_run(host, user, password, key,
                          "hostname; echo '---CRON---'; crontab -l 2>/dev/null || true")
        if not ok:
            log(f"{host}: SKIPPED - {out}")
            results.append({"host": host, "ok": False, "error": out, "jobs": 0})
            continue

        hostname, _, crontab = out.partition("---CRON---")
        hostname = hostname.strip().splitlines()[0] if hostname.strip() else None
        rows = cron_parse.parse_crontab(crontab, known)

        logs = stat_logs(host, user, password, key,
                         sorted({r["log_file"] for r in rows if r["log_file"]}))
        for r in rows:
            if r["log_file"] in logs:
                r["log_mtime"], r["log_size"] = logs[r["log_file"]]

        if not dry_run:
            store.replace_cron_jobs(host, hostname, rows)
        total += len(rows)
        log(f"{host} ({hostname}): {len(rows)} jobs, "
            f"{sum(1 for r in rows if r['partner'])} matched to a partner")
        results.append({"host": host, "hostname": hostname, "ok": True,
                        "jobs": len(rows)})

    return {"hosts": results, "total": total,
            "reachable": sum(1 for r in results if r["ok"])}
