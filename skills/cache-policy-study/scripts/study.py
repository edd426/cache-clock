#!/usr/bin/env python3
"""One command: collect every AI tool's local history, choose the cache-clock policy, write the report.

    python3 study.py --out DIR [collect.py args, e.g. --since 2026-06-01 --only claude-code,codex]
                     [--ttl auto|5m|1h] [--seed N] [--pricing FILE]

Prints the five-line summary and the report path.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import analyze  # noqa: E402
import report  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True)
    ap.add_argument("--ttl", default="auto", choices=["auto", "5m", "1h"])
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--pricing", default=str(analyze.DEFAULT_PRICING))
    a, collect_args = ap.parse_known_args(argv)
    out = Path(a.out)
    rc = subprocess.call([sys.executable, "-I", str(HERE / "collect.py"), "--out", str(out)] + collect_args)
    if rc != 0:
        print(f"collect.py failed (exit {rc})", file=sys.stderr)
        return rc
    if analyze.main(["--run", str(out), "--ttl", a.ttl, "--seed", str(a.seed), "--pricing", a.pricing]) != 0:
        return 1
    report.main(["--run", str(out)])
    print(f"report: {(out / 'report.html').resolve()}")
    return 0


if __name__ == "__main__":
    from cps_common import utf8_console
    utf8_console()
    sys.exit(main())
