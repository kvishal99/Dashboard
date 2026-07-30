"""Read-only MySQL access for the partner counts.

This module is the only place the dashboard talks to MySQL, and it is
deliberately locked down: `scalar()` refuses to execute anything that is not a
single bare SELECT. Statements are also run inside a READ ONLY transaction, so
even a query that somehow slipped past the guard could not modify data.

Nothing in this project ever INSERTs, UPDATEs, DELETEs, DROPs or ALTERs on the
partner database.
"""
import re
from typing import Any, Dict, Optional

import pymysql

# A statement is allowed only if it starts with SELECT ...
_SELECT_START = re.compile(r"^\s*select\s", re.IGNORECASE)

# ... and mentions none of these anywhere, which also blocks a stacked
# "SELECT 1; DROP TABLE x" payload.
_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|truncate|replace|create|grant|revoke|"
    r"rename|lock|unlock|call|handler|load\s+data|into\s+outfile|into\s+dumpfile|set\s)\b",
    re.IGNORECASE,
)


class UnsafeQuery(ValueError):
    """Raised when a configured query is not a plain read."""


def assert_read_only(sql: str) -> None:
    """Reject anything that is not a single, bare SELECT."""
    if ";" in sql.rstrip().rstrip(";"):
        raise UnsafeQuery("multiple statements are not allowed")
    if not _SELECT_START.match(sql):
        raise UnsafeQuery("query must start with SELECT")
    match = _FORBIDDEN.search(sql)
    if match:
        raise UnsafeQuery(f"forbidden keyword in query: {match.group(0).strip()!r}")


def connect(db: Dict[str, Any]) -> pymysql.connections.Connection:
    return pymysql.connect(
        host=db["host"],
        port=int(db.get("port", 3306)),
        user=db["user"],
        password=db.get("password") or "",
        database=db["database"],
        connect_timeout=int(db.get("connect_timeout", 10)),
        read_timeout=int(db.get("read_timeout", 180)),
        charset=db.get("charset", "utf8mb4"),
        autocommit=True,
    )


def scalar(db: Dict[str, Any], sql: str, params: tuple = ()) -> Optional[int]:
    """Run one guarded SELECT and return its first column as an int."""
    assert_read_only(sql)
    conn = connect(db)
    try:
        with conn.cursor() as cur:
            # Belt and braces: the server itself refuses writes on this session.
            cur.execute("SET SESSION TRANSACTION READ ONLY")
            cur.execute(sql, params)
            row = cur.fetchone()
    finally:
        conn.close()
    if not row or row[0] is None:
        return None
    return int(row[0])


def rows(db: Dict[str, Any], sql: str, params: tuple = ()) -> list:
    """Run one guarded SELECT and return every row. Same guard as scalar()."""
    assert_read_only(sql)
    conn = connect(db)
    try:
        with conn.cursor() as cur:
            cur.execute("SET SESSION TRANSACTION READ ONLY")
            cur.execute(sql, params)
            return list(cur.fetchall())
    finally:
        conn.close()


def ping(db: Dict[str, Any]) -> Dict[str, Any]:
    """Connectivity check, and - importantly - WHICH server answered.

    The hostname/uuid matter: a laptop copy and the production master both
    answer on 127.0.0.1:3306 and both have an `admin` schema, so without an
    identity check it is very easy to read the wrong numbers and not notice.
    """
    try:
        conn = connect(db)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT VERSION(), @@hostname, @@server_uuid, @@port")
                version, hostname, uuid, port = cur.fetchone()
        finally:
            conn.close()
        return {
            "ok": True,
            "version": version,
            "hostname": hostname,
            "server_uuid": uuid,
            "port": port,
            "connected_to": f"{db.get('host')}:{db.get('port')}",
            "error": None,
        }
    except Exception as exc:
        return {
            "ok": False, "version": None, "hostname": None, "server_uuid": None,
            "port": None, "connected_to": f"{db.get('host')}:{db.get('port')}",
            "error": f"{type(exc).__name__}: {exc}",
        }
