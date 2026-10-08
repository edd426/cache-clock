"""Shared helpers for cache-policy-study. Python 3.9+, standard library only."""
from __future__ import annotations

import hashlib
import json
import os
import platform
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional

KINDS = {"prompt", "auto_prompt", "response", "turn_end", "session_start", "session_end", "activity", "compaction"}
FIDELITIES = {"turn", "session", "mtime"}


@dataclass
class Env:
    """Where to look. Placeholders resolve per OS; an unset placeholder means the root is skipped."""
    home: Path
    appdata: Optional[Path]        # Windows %APPDATA%
    localappdata: Optional[Path]   # Windows %LOCALAPPDATA%
    system: str                    # 'Darwin', 'Windows', 'Linux'
    environ: Dict[str, str]

    @classmethod
    def detect(cls, home: Optional[str] = None) -> "Env":
        env = dict(os.environ)
        sysname = platform.system()
        return cls(
            home=Path(home or env.get("USERPROFILE") or env.get("HOME") or str(Path.home())),
            appdata=Path(env["APPDATA"]) if env.get("APPDATA") else None,
            localappdata=Path(env["LOCALAPPDATA"]) if env.get("LOCALAPPDATA") else None,
            system=sysname,
            environ=env,
        )

    def app_support(self) -> Optional[Path]:
        """Per-user application data folder for Electron apps (VS Code, Cursor, Antigravity)."""
        if self.system == "Windows":
            return self.appdata
        if self.system == "Darwin":
            return self.home / "Library" / "Application Support"
        return Path(self.environ.get("XDG_CONFIG_HOME") or self.home / ".config")

    def placeholder(self, path: Path) -> str:
        """A path with the home folder replaced, for coverage notes (never print raw user paths)."""
        # Forward slashes on every OS; Windows paths compare case-insensitively.
        s = path.as_posix()
        fold = (lambda x: x.lower()) if self.system == "Windows" else (lambda x: x)
        for base, name in ((self.appdata, "{APPDATA}"), (self.localappdata, "{LOCALAPPDATA}"), (self.home, "{HOME}")):
            if base and fold(s).startswith(fold(base.as_posix())):
                return name + s[len(base.as_posix()):]
        return s


@dataclass
class Coverage:
    tool: str
    verified: bool                       # the adapter was checked against real data from this tool's store
    roots_checked: List[str] = field(default_factory=list)
    roots_found: List[str] = field(default_factory=list)
    files_read: int = 0
    files_skipped: Dict[str, int] = field(default_factory=dict)   # reason -> count
    parse_errors: int = 0
    notes: List[str] = field(default_factory=list)

    def skip(self, reason: str) -> None:
        self.files_skipped[reason] = self.files_skipped.get(reason, 0) + 1

    def note(self, text: str) -> None:
        if text not in self.notes:
            self.notes.append(text)


def parse_time(value: Any) -> Optional[float]:
    """ISO-8601 string, epoch seconds, or epoch milliseconds -> UTC epoch seconds."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        return v / 1000.0 if v > 1e11 else v
    if isinstance(value, str):
        s = value.strip()
        if s.isdigit():
            return parse_time(int(s))
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    return None


def project_id(path_or_name: Optional[str]) -> Optional[str]:
    if not path_or_name:
        return None
    return hashlib.sha1(str(path_or_name).encode("utf-8", "replace")).hexdigest()[:10]


def in_window(t: float, since: Optional[float], until: Optional[float]) -> bool:
    return (since is None or t >= since) and (until is None or t < until)


def event(tool: str, session: str, t: float, kind: str, *, interactive: Optional[bool] = None,
          fidelity: str = "turn", project: Optional[str] = None, model: Optional[str] = None,
          subagent: bool = False, usage: Optional[Dict[str, Optional[int]]] = None,
          compaction: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    assert kind in KINDS, kind
    assert fidelity in FIDELITIES, fidelity
    return {"tool": tool, "session": session, "t": round(float(t), 3), "kind": kind, "interactive": interactive,
            "fidelity": fidelity, "project": project, "model": model, "subagent": subagent,
            "usage": usage, "compaction": compaction}


def usage(input: Optional[int] = None, output: Optional[int] = None, cache_read: Optional[int] = None,
          cache_write: Optional[int] = None, cache_write_5m: Optional[int] = None,
          cache_write_1h: Optional[int] = None, ctx: Optional[int] = None) -> Dict[str, Optional[int]]:
    return {"input": input, "output": output, "cache_read": cache_read, "cache_write": cache_write,
            "cache_write_5m": cache_write_5m, "cache_write_1h": cache_write_1h, "ctx": ctx}


# Spend ledger: every response's tokens, by tool, class and UTC day, so the report can say what share of all
# spend a saving is. Built before collect.py drops subagent and scripted-run events from events.jsonl.
SPEND_FIELDS = ("input", "output", "cache_read", "cache_write_5m", "cache_write_1h", "cache_write_other")
SPEND_CLASSES = ("main", "subagent", "headless")


def spend_class(e: Dict[str, Any]) -> str:
    """headless wins (a scripted run's subagents are scripted too); unknown interactivity counts as interactive."""
    if e.get("interactive") is False:
        return "headless"
    return "subagent" if e.get("subagent") else "main"


def spend_add(ledger: Dict[Any, Dict[str, int]], e: Dict[str, Any]) -> None:
    if e.get("kind") != "response":
        return
    day = datetime.fromtimestamp(e["t"], tz=timezone.utc).strftime("%Y-%m-%d")
    a = ledger.setdefault((e["tool"], spend_class(e), day), dict.fromkeys(("responses", "with_usage") + SPEND_FIELDS, 0))
    a["responses"] += 1
    u = e.get("usage") or {}
    if not any(u.get(f) for f in ("input", "output", "cache_read", "cache_write")):
        return                     # no token counts in this store (or all zero): counted, not priced
    a["with_usage"] += 1
    w5, w1 = u.get("cache_write_5m") or 0, u.get("cache_write_1h") or 0
    for f in ("input", "output", "cache_read"):
        a[f] += u.get(f) or 0
    a["cache_write_5m"] += w5
    a["cache_write_1h"] += w1
    a["cache_write_other"] += max(0, (u.get("cache_write") or 0) - w5 - w1)


def spend_rows(ledger: Dict[Any, Dict[str, int]]) -> List[Dict[str, Any]]:
    return [{"tool": t, "class": c, "day": d, **v} for (t, c, d), v in sorted(ledger.items())]


def iter_jsonl(path: Path, cov: Optional[Coverage] = None) -> Iterator[Dict[str, Any]]:
    """Yield JSON objects from a JSONL file, counting (not raising on) bad lines."""
    try:
        f = open(path, "rb")
    except OSError:
        if cov:
            cov.skip("unreadable")
        return
    with f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except ValueError:
                if cov:
                    cov.parse_errors += 1
                continue
            if isinstance(obj, dict):
                yield obj


def write_jsonl(path: Path, rows: Iterable[Dict[str, Any]]) -> int:
    n = 0
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, separators=(",", ":")) + "\n")
            n += 1
    return n


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    return list(iter_jsonl(path))


def eprint(*a: Any) -> None:
    print(*a, file=sys.stderr)


def utf8_console() -> None:
    """Legacy Windows console code pages cannot print the report's ≤, →, ±."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
