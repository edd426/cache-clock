#!/usr/bin/env python3
"""Read every supported AI-tool history store on this machine into one events.jsonl (read-only).

    python3 collect.py --out ./cps-YYYY-MM-DD [--only claude-code,codex] [--since 2026-06-01] [--include-headless]

Writes events.jsonl, coverage.json and (when power.py finds a sleep log) sleep.json into --out.
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cps_common import Coverage, Env, eprint, parse_time, write_jsonl  # noqa: E402
import adapters  # noqa: E402


def month(t: float) -> str:
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m")


def summarize(tool_events, cov: Coverage, status: str, error: str = None) -> dict:
    sessions = {}
    for e in tool_events:
        s = sessions.setdefault(e["session"], {"first": e["t"], "interactive": e["interactive"]})
        s["first"] = min(s["first"], e["t"])
        if e["interactive"] is not None:
            s["interactive"] = e["interactive"]
    per_month = collections.Counter(month(s["first"]) for s in sessions.values())
    kinds = collections.Counter(e["kind"] for e in tool_events)
    fid = collections.Counter(e["fidelity"] for e in tool_events)
    ts = [e["t"] for e in tool_events]
    return {
        "tool": cov.tool, "status": status, "error": error, "verified_adapter": cov.verified,
        "roots_checked": cov.roots_checked, "roots_found": cov.roots_found,
        "files_read": cov.files_read, "files_skipped": cov.files_skipped, "parse_errors": cov.parse_errors,
        "sessions": len(sessions),
        "interactive_sessions": sum(1 for s in sessions.values() if s["interactive"] is True),
        "headless_sessions": sum(1 for s in sessions.values() if s["interactive"] is False),
        "events": len(tool_events), "kinds": dict(kinds), "fidelity": dict(fid),
        "first": datetime.fromtimestamp(min(ts), tz=timezone.utc).isoformat() if ts else None,
        "last": datetime.fromtimestamp(max(ts), tz=timezone.utc).isoformat() if ts else None,
        "per_month_sessions": dict(sorted(per_month.items())),
        "notes": cov.notes,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True)
    ap.add_argument("--only", help="comma-separated tool ids (default: all)")
    ap.add_argument("--since"), ap.add_argument("--until")
    ap.add_argument("--home", help="read another user's home folder (default: this user's)")
    ap.add_argument("--include-headless", action="store_true",
                    help="keep events from scripted runs (claude -p, codex exec); they are counted either way")
    ap.add_argument("--no-power", action="store_true", help="skip reading the OS sleep/wake log")
    a = ap.parse_args(argv)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    env = Env.detect(a.home)
    since, until = parse_time(a.since), parse_time(a.until)
    only = set(a.only.split(",")) if a.only else None

    all_events, coverage = [], []
    for name in adapters.MODULES:
        try:
            mod = adapters.load(name)
        except ImportError as exc:
            coverage.append({"tool": name.replace("_", "-"), "status": "not-built", "error": str(exc)})
            continue
        if only and mod.TOOL not in only:
            continue
        cov = Coverage(mod.TOOL, mod.VERIFIED)
        t0 = time.time()
        evs, status, err = [], "ok", None
        try:
            evs = list(mod.collect(env, since, until, cov))
        except Exception as exc:  # an adapter bug must not sink the study
            status, err = "error", f"{type(exc).__name__}: {exc}"
            eprint(traceback.format_exc())
        if status == "ok" and not cov.roots_found:
            status = "absent"
        kept = evs if a.include_headless else [e for e in evs if e["interactive"] is not False]
        row = summarize(evs, cov, status, err)
        row["events_kept"] = len(kept)
        row["seconds"] = round(time.time() - t0, 1)
        coverage.append(row)
        all_events.extend(kept)
        eprint(f"{mod.TOOL:<15} {status:<7} sessions={row['sessions']:<6} interactive={row['interactive_sessions']:<5} events kept={len(kept)}")

    all_events.sort(key=lambda e: e["t"])
    write_jsonl(out / "events.jsonl", all_events)
    meta = {"generated": datetime.now(timezone.utc).isoformat(), "system": env.system,
            "since": a.since, "until": a.until, "include_headless": a.include_headless, "tools": coverage}

    if not a.no_power:
        try:
            import power
            pcov = Coverage("power", getattr(power, "VERIFIED", False))
            intervals = power.sleep_intervals(env, since, until, pcov)
            # The span the log speaks for: its first record until now (this process runs, so the machine is
            # awake now). Stretches outside it have no sleep evidence either way.
            first = getattr(pcov, "first_record", None)
            span = [round(first, 3), round(time.time(), 3)] if first is not None else None
            (out / "sleep.json").write_text(json.dumps({"intervals": intervals, "window": span,
                                                        "roots_checked": pcov.roots_checked,
                                                        "notes": pcov.notes}, indent=1))
            meta["power"] = {"status": "ok" if pcov.roots_found else "absent", "intervals": len(intervals),
                             "notes": pcov.notes}
        except ImportError:
            meta["power"] = {"status": "not-built"}
        except Exception as exc:
            meta["power"] = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
    (out / "coverage.json").write_text(json.dumps(meta, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
