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
import base64
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zlib
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
    """Run a command over SSH. Password goes via SSH_ASKPASS, never argv.

    Password auth is wrapped in `setsid` so ssh has no controlling terminal.
    Without that, ssh prompts on the tty and this hangs until the timeout:
    SSH_ASKPASS_REQUIRE=force only exists in OpenSSH >= 8.4, and Rocky 8 ships
    8.0, where it is ignored. No tty means askpass is the only way in, on every
    version.
    """
    args = ["ssh", *SSH_BASE, "-n"]
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
        # -w so we still get ssh's own exit status and its stdout back.
        setsid = shutil.which("setsid")
        if setsid:
            args = [setsid, "-w"] + args
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



# How many script heads to ask for in one SSH round trip. The whole list at
# once overflows the command line the server will accept (402 paths failed
# outright); 30 is comfortably under it and still only ~14 round trips.
SCRIPT_BATCH = 30
SCRIPT_HEAD_BYTES = 2500

_FILE_MARK = re.compile(r"<<<F:(.*?)>>>\n?")
# A .sh wrapper that just calls a .php - the shell file says nothing about what
# the job does, so the php it runs is read instead.
_PHP_CALL = re.compile(r"(/[^\s;|>'\"]+\.php)")


def read_scripts(host: str, user: str, password: Optional[str], key: Optional[str],
                 paths: List[str]) -> Dict[str, str]:
    """Signature tokens grepped out of each script, keyed by path.

    Paths are guessed; contents are not. `venuepilot_nodejs/venue.sh` sits in a
    partner directory and looks like an ingest job, but it greps netstat for a
    port and restarts a Next.js app - which only reading it can tell you.

    The whole file is scanned server-side and only the matched keywords come
    back. Reading a fixed-size head instead was measurably wrong: the real
    ingest scripts open with includes and configuration, so their `INSERT INTO`
    sits well past any head worth transferring.
    """
    wanted = [p for p in paths if p and "'" not in p]
    bodies: Dict[str, str] = {}
    for i in range(0, len(wanted), SCRIPT_BATCH):
        chunk = wanted[i:i + SCRIPT_BATCH]
        cmd = "; ".join(
            f"echo '<<<F:{p}>>>'; "
            f"grep -hoiE '{cron_parse.SIGNATURE_TOKENS}' '{p}' 2>/dev/null "
            f"| sort -u | head -25"
            for p in chunk
        )
        ok, out = ssh_run(host, user, password, key, cmd, timeout=180)
        if not ok:
            continue
        parts = _FILE_MARK.split(out)
        for j in range(1, len(parts), 2):
            bodies[parts[j]] = parts[j + 1]
    return bodies


def read_raw(host: str, user: str, password: Optional[str], key: Optional[str],
             paths: List[str]) -> Dict[str, str]:
    """The head of each script, used only to follow a wrapper to what it runs."""
    wanted = [p for p in paths if p and "'" not in p]
    out_map: Dict[str, str] = {}
    for i in range(0, len(wanted), SCRIPT_BATCH):
        chunk = wanted[i:i + SCRIPT_BATCH]
        cmd = "; ".join(
            f"echo '<<<F:{p}>>>'; head -c {SCRIPT_HEAD_BYTES} '{p}' 2>/dev/null"
            for p in chunk
        )
        ok, out = ssh_run(host, user, password, key, cmd, timeout=120)
        if not ok:
            continue
        parts = _FILE_MARK.split(out)
        for j in range(1, len(parts), 2):
            out_map[parts[j]] = parts[j + 1]
    return out_map


def fetch_output(host: str, path: str, max_bytes: int = 25 * 1024 * 1024,
                 whole: bool = False) -> Tuple[bool, Any]:
    """Fetch a cron job's output file from its server.

    Returns (True, bytes) or (False, "reason").

    **The tail is the default, not the whole file.** These are append-only run
    logs and several are enormous - `daily-event-cron.sh` writes a 426 MB file
    and `insertSphinxTable.php` a 322 MB one. Pulling those whole over SSH to
    read the last run would take minutes and tell you nothing the last few MB
    do not. `whole=True` asks for the entire file and is refused above
    max_bytes rather than silently truncated, because a CSV cut off mid-row
    looks like a complete file with missing records.

    The file is base64'd in transit: these outputs are CSVs and logs that may
    contain anything, and a raw stream would be mangled by the shell.
    """
    env = env_from_dotenv()
    user, password, key = creds(env, host)
    if "'" in path:
        return False, "refusing a path containing a quote"

    if whole:
        cmd = (f"if [ ! -f '{path}' ]; then echo MISSING; "
               f"elif [ $(stat -c%s '{path}') -gt {max_bytes} ]; then echo TOOBIG; "
               f"else base64 '{path}'; fi")
    else:
        cmd = (f"if [ ! -f '{path}' ]; then echo MISSING; "
               f"else tail -c {max_bytes} '{path}' | base64; fi")

    ok, out = ssh_run(host, user, password, key, cmd, timeout=180)
    if not ok:
        return False, out
    head = out.strip()[:8]
    if head.startswith("MISSING"):
        return False, "that file no longer exists on the server"
    if head.startswith("TOOBIG"):
        return False, (
            f"the file is larger than the {max_bytes // 1024 // 1024} MB limit for a "
            "direct download - use Prepare whole file, which fetches it in the "
            "background"
        )
    try:
        return True, base64.b64decode(out)
    except Exception as exc:
        return False, f"could not decode the file: {exc}"


def remote_size(host: str, path: str) -> Tuple[bool, Any]:
    """Current size of a remote file, re-stat-ed rather than trusted from the
    last collection - these files grow between crontab sweeps."""
    env = env_from_dotenv()
    user, password, key = creds(env, host)
    if "'" in path:
        return False, "refusing a path containing a quote"
    ok, out = ssh_run(host, user, password, key,
                      f"stat -c%s '{path}' 2>/dev/null || echo MISSING", timeout=60)
    if not ok:
        return False, out
    text = out.strip()
    if not text or text.startswith("MISSING"):
        return False, "that file no longer exists on the server"
    try:
        return True, int(text.splitlines()[-1])
    except ValueError:
        return False, f"unexpected stat output: {text[:80]}"


def stream_to_disk(host: str, path: str, dest: str,
                   progress=None, max_bytes: int = 0) -> Tuple[bool, Any]:
    """Copy a whole remote file to `dest`, however large it is.

    Blocking - run in a thread. Returns (True, bytes_written) or (False, reason).

    **Gzipped in transit and decompressed here.** These are run logs and SQL
    dumps, which are hugely repetitive: the 426 MB `daily-event-cron.sh` output
    moves in a small fraction of that. `gzip -1` is used rather than the default
    level because the bottleneck is the link, not the ratio, and level 1 keeps
    the server's CPU out of the way.

    Nothing is ever held in memory: the ssh pipe is read in chunks, decompressed
    incrementally and written straight out, so a 400 MB file costs the same
    memory as a 4 KB one.
    """
    env = env_from_dotenv()
    user, password, key = creds(env, host)
    if "'" in path:
        return False, "refusing a path containing a quote"

    args = ["ssh", *SSH_BASE, "-n"]
    child_env = dict(os.environ)
    askpass_path = None

    if key and os.path.isfile(key):
        args += ["-i", key, "-o", "BatchMode=yes"]
    elif password:
        fd, askpass_path = tempfile.mkstemp(prefix="ops-askpass-", suffix=".sh")
        with os.fdopen(fd, "w") as fh:
            fh.write('#!/usr/bin/env bash\nprintf %s "$OPS_SSH_PASSWORD"\n')
        os.chmod(askpass_path, 0o700)
        child_env.update({
            "OPS_SSH_PASSWORD": password,
            "SSH_ASKPASS": askpass_path,
            "SSH_ASKPASS_REQUIRE": "force",
            "DISPLAY": child_env.get("DISPLAY", ":0"),
        })
        args += ["-o", "PreferredAuthentications=password",
                 "-o", "PubkeyAuthentication=no",
                 "-o", "NumberOfPasswordPrompts=1"]
        setsid = shutil.which("setsid")
        if setsid:
            args = [setsid, "-w"] + args
    else:
        return False, "no password or key configured for that server"

    args += [f"{user}@{host}", f"gzip -1 -c '{path}'"]

    written = 0
    decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)   # 16 = gzip header
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    proc = None
    try:
        proc = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=child_env,
        )
        with open(dest, "wb") as out:
            while True:
                chunk = proc.stdout.read(256 * 1024)
                if not chunk:
                    break
                data = decompressor.decompress(chunk)
                if data:
                    out.write(data)
                    written += len(data)
                    if progress:
                        progress(written)
                if max_bytes and written > max_bytes:
                    proc.kill()
                    return False, (
                        f"the file is larger than the "
                        f"{max_bytes // 1024 // 1024} MB limit for a single transfer"
                    )
            tail = decompressor.flush()
            if tail:
                out.write(tail)
                written += len(tail)

        proc.wait(timeout=60)
        if proc.returncode != 0:
            err = (proc.stderr.read() or b"").decode("utf-8", "replace").strip()
            return False, err.splitlines()[-1] if err else "ssh failed"
        if progress:
            progress(written)
        return True, written
    except Exception as exc:
        if proc:
            proc.kill()
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        if askpass_path:
            os.unlink(askpass_path)


def classify_jobs(host: str, user: str, password: Optional[str], key: Optional[str],
                  rows: List[Dict[str, Any]], log=print) -> int:
    """Re-categorise rows from their scripts' contents. Returns how many changed.

    Falls back silently to the path-derived category: a host that refuses the
    read, or a wrapper whose body says nothing, must not lose the category it
    already had.
    """
    paths = sorted({r["script"] for r in rows if r.get("script")})
    tokens = read_scripts(host, user, password, key, paths)
    if not tokens:
        return 0

    # A shell wrapper often just runs a php file and matches nothing itself, so
    # the php it calls is scanned as well - one level, which covers the
    # `partner_cron.sh -> insertEvents-partner.php` shape these crontabs use.
    silent = [p for p in paths
              if p.endswith(".sh") and not cron_parse.categorise_tokens(
                  (tokens.get(p) or "").splitlines())]
    called: Dict[str, List[str]] = {}
    if silent:
        heads = read_raw(host, user, password, key, silent)
        followups = set()
        for path, head in heads.items():
            targets = [m for m in _PHP_CALL.findall(head)][:4]
            called[path] = targets
            followups.update(t for t in targets if t not in tokens)
        if followups:
            tokens.update(read_scripts(host, user, password, key, sorted(followups)))

    changed = 0
    for row in rows:
        script = row.get("script") or ""
        category = cron_parse.categorise_tokens((tokens.get(script) or "").splitlines())

        if not category:
            for target in called.get(script, []):
                category = cron_parse.categorise_tokens(
                    (tokens.get(target) or "").splitlines())
                if category:
                    break

        if category:
            if category != row.get("category"):
                changed += 1
            row["category"] = category
            row["category_source"] = "content"
        else:
            row["category_source"] = "path"
    return changed


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

    # Both sources: the status sheet misses partners that are live in MySQL,
    # and MySQL misses partners that have a cron job but no rows. Passing only
    # partner_meta - which this used to do - discarded the 120 partners already
    # discovered from the database.
    known = set(config.partner_meta.keys()) | set(store.latest_counts().keys())
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

        # Read the scripts and let their contents overrule the path guess. This
        # is what stops a website watchdog in a partner-named directory from
        # being filed as a data insertion.
        recategorised = classify_jobs(host, user, password, key, rows, log)

        if not dry_run:
            store.replace_cron_jobs(host, hostname, rows)
        total += len(rows)
        by_content = sum(1 for r in rows if r.get("category_source") == "content")
        log(f"{host} ({hostname}): {len(rows)} jobs, "
            f"{sum(1 for r in rows if r['partner'])} matched to a partner, "
            f"{by_content} categorised from script contents "
            f"({recategorised} corrected)")
        results.append({"host": host, "hostname": hostname, "ok": True,
                        "jobs": len(rows)})

    return {"hosts": results, "total": total,
            "reachable": sum(1 for r in results if r["ok"])}
