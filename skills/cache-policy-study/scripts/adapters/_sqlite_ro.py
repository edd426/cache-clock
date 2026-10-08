"""Open a tool's SQLite store read-only without disturbing the app that owns it (not an adapter).

Order: a database with a non-empty -wal is opened `mode=ro` (reads the committed WAL frames; immutable=1 may
skip them, losing the newest turns); otherwise `mode=ro&immutable=1` (no locks, no change detection). If that
fails (the app holds an exclusive lock, -shm missing and not creatable), the database and its -wal/-shm are
copied to a temp dir and the copy is opened. The original is never written.
"""
from __future__ import annotations

import contextlib
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Iterator, Optional
from urllib.parse import quote

from cps_common import Coverage


def _uri(path: Path, params: str) -> str:
    p = path.resolve().as_posix()   # 'C:/Users/...' on Windows -> file:///C:/Users/...
    return "file://" + ("" if p.startswith("/") else "/") + quote(p, safe="/:") + "?" + params


def _try(uri: str) -> Optional[sqlite3.Connection]:
    try:
        con = sqlite3.connect(uri, uri=True, timeout=2)
        con.execute("SELECT count(*) FROM sqlite_master").fetchone()
        return con
    except sqlite3.Error:
        return None


@contextlib.contextmanager
def open_ro(path: Path, cov: Coverage) -> Iterator[Optional[sqlite3.Connection]]:
    wal = path.with_name(path.name + "-wal")
    try:
        has_wal = wal.is_file() and wal.stat().st_size > 0
    except OSError:
        has_wal = False
    con = _try(_uri(path, "mode=ro")) if has_wal else _try(_uri(path, "mode=ro&immutable=1"))
    tmp = None
    if con is None:
        try:
            tmp = tempfile.mkdtemp(prefix="cps-")
            for suffix in ("", "-wal", "-shm"):
                src = path.with_name(path.name + suffix)
                if src.is_file():
                    shutil.copy2(src, Path(tmp) / (path.name + suffix))
            con = _try(_uri(Path(tmp) / path.name, "mode=ro"))
            if con is not None:
                cov.note("some databases were locked; read from a temporary copy")
        except OSError:
            con = None
    if con is None:
        cov.skip("sqlite-unopenable")
    try:
        yield con
    finally:
        if con is not None:
            con.close()
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
