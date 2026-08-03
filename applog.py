"""Reading the dashboard's own log files for the Logs page.

Two things make this less trivial than opening a file.

**Tail-first reading.** A log left running for a month is tens of megabytes, and
the page wants the newest lines. Reading the whole file to show the last 200
lines would make the page slower the longer the dashboard has been up - exactly
backwards. `_tail_bytes` seeks to the end and reads backwards in blocks, so cost
is set by how much is displayed, not by how much has accumulated.

**Two line formats.** Uvicorn's default output carries no timestamp at all
(`INFO:     127.0.0.1 - "GET / HTTP/1.1" 200 OK`), which makes filtering by date
impossible. `configure()` installs a formatter that puts one on every future
line, and the parser reads both - so existing logs stay readable instead of the
page starting empty after the change.
"""
import logging
import os
import re
import time
from datetime import datetime
from typing import Any, Dict, Iterator, List, Optional

# What every line written from now on looks like. The separator is " | " rather
# than whitespace so a message containing spaces can never be mistaken for a
# field, which is what makes the parse below unambiguous.
LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

# The format configure() installs.
_STRUCTURED = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s*\|\s*"
    r"(?P<level>[A-Z]+)\s*\|\s*(?P<logger>[^|]*?)\s*\|\s*(?P<message>.*)$"
)
# Uvicorn's default, and anything else that at least names its level first.
_LEVEL_ONLY = re.compile(r"^(?P<level>DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL):\s*(?P<message>.*)$")

# A request line inside a uvicorn access message, so the Logs page can filter by
# status class without the user writing a regex.
_ACCESS = re.compile(r'"(?P<method>[A-Z]+) (?P<path>\S+) HTTP/[\d.]+"\s+(?P<status>\d{3})')


def configure(log_path: Optional[str] = None, level: str = "INFO") -> None:
    """Timestamp every log line, and mirror it to a file if one is given.

    Called once at startup. Uvicorn configures its own loggers, so this
    re-formats the existing handlers rather than adding a second set - otherwise
    every line would appear twice.
    """
    formatter = logging.Formatter(LOG_FORMAT, DATE_FORMAT)
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    for name in ("", "uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        for handler in logger.handlers:
            handler.setFormatter(formatter)

    if not log_path:
        return

    # The file handler goes on the ROOT logger only, and uvicorn's loggers are
    # made to propagate up to it.
    #
    # Attaching it to both - which is the obvious thing to do, since uvicorn
    # configures its loggers itself - writes every line twice: once by the
    # logger's own handler and once again after propagation. Uvicorn keeps its
    # console handlers either way, so output is unaffected.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).propagate = True

    # Idempotent: configure() may be called again on a reload, and a second
    # handler on the same file would reintroduce the duplication above.
    target = os.path.abspath(log_path)
    for handler in root.handlers:
        if os.path.abspath(getattr(handler, "baseFilename", "")) == target:
            return

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)


def _tail_bytes(path: str, max_bytes: int) -> str:
    """The last `max_bytes` of a file, starting at a line boundary."""
    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        if size > max_bytes:
            fh.seek(size - max_bytes)
            fh.readline()          # discard the partial first line
        data = fh.read()
    return data.decode("utf-8", errors="replace")


def parse_line(raw: str, line_no: int, source: str) -> Dict[str, Any]:
    """One log line as a row. Never fails - an unparseable line is still a line."""
    text = raw.rstrip("\n")
    entry: Dict[str, Any] = {
        "line": line_no, "source": source, "raw": text,
        "ts": None, "time": None, "level": "INFO", "logger": "", "message": text,
        "method": None, "path": None, "status": None,
    }

    match = _STRUCTURED.match(text)
    if match:
        entry["time"] = match.group("ts")
        entry["level"] = match.group("level")
        entry["logger"] = match.group("logger")
        entry["message"] = match.group("message")
        try:
            entry["ts"] = datetime.strptime(match.group("ts"), DATE_FORMAT).timestamp()
        except ValueError:
            pass
    else:
        match = _LEVEL_ONLY.match(text)
        if match:
            entry["level"] = "WARNING" if match.group("level") == "WARN" else match.group("level")
            entry["message"] = match.group("message")

    access = _ACCESS.search(entry["message"])
    if access:
        entry["method"] = access.group("method")
        entry["path"] = access.group("path")
        entry["status"] = int(access.group("status"))
        # An HTTP error is a log error regardless of the level uvicorn used -
        # otherwise a page of 500s reads as INFO and filtering by level hides
        # the one thing worth finding.
        if entry["status"] >= 500:
            entry["level"] = "ERROR"
        elif entry["status"] >= 400 and entry["level"] == "INFO":
            entry["level"] = "WARNING"
    return entry


def available(base_dir: str, extra: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Every log file this dashboard can show, newest first.

    Only files this process writes or was told about - the page never walks the
    filesystem looking for logs it has no business reading.
    """
    candidates: List[str] = []
    for name in sorted(os.listdir(base_dir)):
        if name.endswith(".log"):
            candidates.append(os.path.join(base_dir, name))
    for path in (extra or []):
        if path and path not in candidates:
            candidates.append(path)

    files = []
    for path in candidates:
        try:
            stat = os.stat(path)
        except OSError:
            continue                       # configured but not created yet
        files.append({
            "name": os.path.basename(path),
            "path": path,
            "size": stat.st_size,
            "modified": stat.st_mtime,
        })
    files.sort(key=lambda f: -f["modified"])
    return files


def read(
    path: str,
    query: str = "",
    level: str = "",
    since: Optional[float] = None,
    until: Optional[float] = None,
    status_class: str = "",
    offset: int = 0,
    limit: int = 200,
    max_bytes: int = 4_000_000,
) -> Dict[str, Any]:
    """Filtered, paginated lines from one log file, newest first.

    `total` is the number of lines matching the filter, not the number in the
    file, so the pager reflects what the filter actually found.
    """
    if not os.path.isfile(path):
        return {"lines": [], "total": 0, "scanned": 0, "truncated": False,
                "error": f"no such log file: {os.path.basename(path)}"}

    size = os.path.getsize(path)
    truncated = size > max_bytes
    text = _tail_bytes(path, max_bytes)
    source = os.path.basename(path)

    needle = query.strip().lower()
    wanted_level = level.strip().upper()
    # Levels are a floor, not an exact match: asking for WARNING means
    # "warnings and worse", which is what someone triaging actually wants.
    floor = LEVELS.index(wanted_level) if wanted_level in LEVELS else None

    matched: List[Dict[str, Any]] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip():
            continue
        entry = parse_line(raw, number, source)

        if floor is not None:
            position = LEVELS.index(entry["level"]) if entry["level"] in LEVELS else 1
            if position < floor:
                continue
        if needle and needle not in entry["raw"].lower():
            continue
        # Undated legacy lines are kept when no date filter is set and dropped
        # when one is - claiming they fall inside a range we cannot check would
        # be a guess.
        if (since or until) and entry["ts"] is None:
            continue
        if since and entry["ts"] < since:
            continue
        if until and entry["ts"] > until:
            continue
        if status_class:
            if entry["status"] is None:
                continue
            if not str(entry["status"]).startswith(status_class[0]):
                continue
        matched.append(entry)

    matched.reverse()                      # newest first
    return {
        "lines": matched[offset:offset + limit],
        "total": len(matched),
        "scanned": size,
        "truncated": truncated,
        "error": None,
    }


def summarise(path: str, max_bytes: int = 2_000_000) -> Dict[str, Any]:
    """Level counts for the tiles above the log viewer."""
    if not os.path.isfile(path):
        return {"total": 0, "errors": 0, "warnings": 0, "info": 0, "first": None, "last": None}

    counts = {"total": 0, "errors": 0, "warnings": 0, "info": 0}
    first = last = None
    for raw in _tail_bytes(path, max_bytes).splitlines():
        if not raw.strip():
            continue
        entry = parse_line(raw, 0, "")
        counts["total"] += 1
        if entry["level"] in ("ERROR", "CRITICAL"):
            counts["errors"] += 1
        elif entry["level"] == "WARNING":
            counts["warnings"] += 1
        else:
            counts["info"] += 1
        if entry["ts"]:
            first = entry["ts"] if first is None else min(first, entry["ts"])
            last = entry["ts"] if last is None else max(last, entry["ts"])
    return {**counts, "first": first, "last": last}


def iter_lines(path: str) -> Iterator[str]:
    """Whole file, for the download route. Streamed, never loaded at once."""
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            yield line
