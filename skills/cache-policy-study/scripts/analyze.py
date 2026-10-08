#!/usr/bin/env python3
"""Choose the cache-clock idle policy that minimises expected prompt-cache cost on your own history.

    python3 analyze.py --run DIR [--pricing FILE] [--ttl auto|5m|1h] [--lead MIN] [--settings FILE]
                       [--seed N] [--resamples N]

Reads DIR/events.jsonl (+ coverage.json, spend.json, optional sleep.json) and writes DIR/results.json.
Two views: Claude Code behaviour alone, and return times pooled over every AI tool found.
Costs are input-token equivalents (base input price = 1). The method is in references/method.md.
"""
from __future__ import annotations

import argparse
import bisect
import collections
import json
import math
import operator
import os
import random
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

HERE = Path(__file__).resolve().parent
DEFAULT_PRICING = HERE.parent / "references" / "pricing.json"
sys.path.insert(0, str(HERE))
from cps_common import SPEND_FIELDS, spend_add, spend_rows  # noqa: E402
import ttl as ttl_mod  # noqa: E402
INF = float("inf")
CC = "claude-code"
TOOL_NAMES = {"claude-code": "Claude Code", "codex": "Codex", "gemini-cli": "Gemini CLI", "antigravity": "Antigravity",
              "vscode-copilot": "VS Code Copilot", "cursor": "Cursor", "copilot-cli": "Copilot CLI"}

A_GRID = [0.0, 50e3, 75e3, 100e3, 125e3, 150e3, 200e3]
B_GRID = [150e3, 200e3, 250e3, 300e3, 400e3, 500e3, 600e3, INF]
N_GRID = [1, 2, 3, 4]
BAND_EDGES = [0.0, 50e3, 75e3, 100e3, 125e3, 150e3, 200e3, 250e3, 300e3, 400e3, 600e3, INF]
TABLE_CTX = [(0.0, 50e3, "<50k"), (50e3, 100e3, "50–100k"), (100e3, 300e3, "100–300k"), (300e3, INF, "≥300k")]
MOD_DEFAULTS = {"A": 125e3, "B": 300e3, "N": 3, "lead": 3.0, "ttl": "1h"}   # cache-clock plugin.json
SENS_LEADS = [2.0, 3.0, 5.0]
MIN_BAND_N = 30
CHANGE_MIN_POINTS = 1.0        # recommend a change only if the paired median gain is at least this …
PRESENCE_KINDS = {"prompt", "response", "activity", "session_start", "session_end"}
DEFAULT_POST_CTX = 60_000      # first request after a compaction, when the user has never compacted
DEFAULT_SUMMARY_OUT = 7_000    # compaction summary output tokens, when nothing can be measured
SUMMARY_PER_POST = 0.5         # summary output ≈ 0.5 × compactMetadata.postTokens (see method.md)
RETURN_LABELS = ("human", "automatic", "within a turn", "other")


# ----------------------------------------------------------------------------- helpers

def k(x: Optional[float]) -> str:
    if x is None or x == INF:
        return "∞"
    return f"{x / 1e3:.0f}k"


def dur(sec: float) -> str:
    if sec == INF:
        return "∞"
    m = sec / 60
    if m < 1:
        return f"{sec:.0f} s"
    if m < 120 and abs(m - round(m)) < 1e-6:
        return f"{m:.0f} m"
    if m < 120:
        return f"{m:.1f} m"
    return f"{m / 60:.0f} h"


def med(xs: Sequence[float]) -> Optional[float]:
    return statistics.median(xs) if xs else None


def pct(xs: Sequence[float], q: float) -> Optional[float]:
    if not xs:
        return None
    s = sorted(xs)
    i = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return s[i]


def saving(c: float, base: float) -> Optional[float]:
    return None if base <= 0 else 1.0 - c / base


def tool_name(t: str) -> str:
    return TOOL_NAMES.get(t, t)


# ----------------------------------------------------------------------------- current settings

def read_current(settings_path: Optional[Path] = None) -> Tuple[Dict[str, Any], str]:
    """The user's cache-clock settings (settings.json → pluginConfigs → cache-clock* → options), else the mod defaults."""
    if settings_path is None:
        base = os.environ.get("CLAUDE_CONFIG_DIR")
        settings_path = Path(base) / "settings.json" if base else Path.home() / ".claude" / "settings.json"
    cur = dict(MOD_DEFAULTS)
    try:
        d = json.loads(Path(settings_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return cur, "mod defaults"
    opts = None
    for key, v in ((d or {}).get("pluginConfigs") or {}).items():
        if str(key).startswith("cache-clock") and isinstance(v, dict):
            opts = v.get("options") if isinstance(v.get("options"), dict) else None
            if opts:
                break
    if not opts:
        return cur, "mod defaults"
    for src, dst, f in (("keepAliveBelowTokens", "A", float), ("compactAboveTokens", "B", float), ("maxKeepAlives", "N", int),
                        ("leadMinutes", "lead", float), ("ttl", "ttl", str)):
        if opts.get(src) is not None:
            try:
                cur[dst] = f(opts[src])
            except (TypeError, ValueError):
                pass
    if cur["B"] >= 1e9:
        cur["B"] = INF
    return cur, "your settings"


# ----------------------------------------------------------------------------- action space

def atom_list() -> List[Tuple[int, int, int]]:
    """(pings, compact, cap): k pings then lapse, or k pings then compact; cap bounds the pings sent in place of a
    compaction the engine refuses mid-turn (the rule's maxKeepAlives)."""
    return [(p, 0, 0) for p in range(6)] + [(p, 1, cap) for p in range(4) for cap in N_GRID]


class Space:
    """Atomic policies and the 3-tier rule grid for one TTL and one (fixed) lead."""

    def __init__(self, T: float, lead: float, current: Dict[str, Any]):
        self.T, self.lead = T, min(lead, T / 120)
        self.atoms = atom_list()
        self.ai = {a: i for i, a in enumerate(self.atoms)}
        self.nothing = self.ai[(0, 0, 0)]
        cur = (float(current["A"]), float(current["B"]), int(current["N"]))
        bps = set(A_GRID) | {b for b in B_GRID if b < INF} | {cur[0]} | ({cur[1]} if cur[1] < INF else set())
        self.bps = sorted(bps)
        self.rules: List[Tuple[float, float, int]] = [(A, B, N) for A in A_GRID for B in B_GRID for N in N_GRID if B > A]
        if cur[1] <= cur[0]:
            cur = (cur[0], INF, cur[2]) if cur[0] > 0 else cur
        if cur not in self.rules:
            self.rules.append(cur)
        self.current = self.rules.index(cur)
        self.rule_seg_atoms = [[self.ai[rule_atom(r, lo)] for lo in self.bps] for r in self.rules]

    def threshold(self) -> float:
        """Earliest the mod could act at any lead considered (the sensitivity leads included)."""
        return self.T - max(SENS_LEADS + [self.lead]) * 60 if self.T >= 1800 else self.T / 2

    def seg_of(self, C: float) -> int:
        return max(0, bisect.bisect_right(self.bps, C) - 1)


def rule_atom(rule: Tuple[float, float, int], C: float) -> Tuple[int, int, int]:
    """cache-clock decide(): ≥B compact at once; A..B one ping then compact; <A up to N pings then lapse."""
    A, B, N = rule
    if C >= B:
        return (0, 1, N)
    if C >= A:
        return (1, 1, N)
    return (N, 0, 0)


def rule_name(rule: Optional[Tuple[float, float, int]], lead: Optional[float] = None) -> str:
    if rule is None:
        return "do nothing"
    A, B, N = rule
    parts = []
    if A > 0:
        parts.append(f"<{k(A)}: ≤{N} ping{'s' if N != 1 else ''}")
    if B > A:
        parts.append(f"{k(A)}–{k(B)}: 1 ping then compact" if B < INF else f"≥{k(A)}: 1 ping then compact")
    if B < INF:
        parts.append(f"≥{k(B)}: compact")
    return " · ".join(parts) + ("" if lead is None else f" · act {lead:g} min early")


def atom_name(a: Tuple[int, int, int]) -> str:
    p, c, cap = a
    if c:
        s = "compact" if p == 0 else f"{p} ping{'s' if p != 1 else ''} then compact"
        return s + f" (≤{cap} ping{'s' if cap != 1 else ''} if mid-turn)"
    return "nothing" if p == 0 else f"{p} ping{'s' if p != 1 else ''}"


# ----------------------------------------------------------------------------- cost model

def cold(X: float, m: Dict[str, float]) -> float:
    """Re-sending X tokens after the entry lapsed: the shared prefix W (system prompt, tools) is still cached."""
    W = m.get("W", 0.0)
    return m["write"] * max(0.0, X - W) + m["read"] * min(X, W)


def cost(g: Optional[float], C: float, pings: int, compact: int, cap: int, T: float, lead: float, m: Dict[str, float],
         te: Optional[float] = 0.0, switch: bool = False,
         asleep: Optional[Callable[[float], Optional[float]]] = None, t0: float = 0.0, manual: bool = False) -> float:
    """Cost of one idle stretch (input-token equivalents) under one atomic policy, as the mod would run it.

    g: seconds from walk-away to the return (None = never returned); C: context at walk-away.
    Actions: the first at T−lead; after a successful ping at p the next at p + T − lead (the mod re-arms from the
    ping). A ping re-reads C and restarts the TTL. A compaction reads C, writes a summary as output, and the
    return writes the post-compaction context. te: seconds after walk-away when the turn ended (None = still
    running); mid-turn the engine refuses a compaction, so the mod pings instead, up to `cap` pings in all.
    switch: the return used another model, so it is cold whatever the pings did. asleep(t) returns the end of
    the sleep interval holding t; an action is lost if the machine sleeps through its deadline, and nothing later
    happens. manual: the stretch ends in the person's own /compact — that compaction is paid in every policy
    (a cache-sharing fork that skips the cache write: 0.1×C warm, 1.0×C uncached cold, + summary + post write)
    unless the mod already compacted, which makes it moot.
    """
    step = T - lead * 60
    c, alive, t, i, sent = 0.0, T, 0.0, 0, 0
    while i < pings or compact:
        t += step
        if g is not None and g <= t:
            break
        if asleep is not None:
            end = asleep(t0 + t)
            if end is not None and end >= t0 + t + lead * 60:
                break
        regular = i < pings
        if not regular and (te is None or te > t):      # compaction refused mid-turn: ping instead
            if sent >= cap:
                break
            regular = None
        if regular is False:
            c += C * m["compact_read"] + m["summary"] * m["out"]
            return c + (0.0 if g is None else cold(m["post"], m))
        c += C * m["read"] + m["ping_out"] * m["out"]
        sent += 1
        alive = t + T
        if regular:
            i += 1
    if g is None:
        return c
    warm = g <= alive and not switch
    if manual:
        return c + C * (m["read"] if warm else m.get("input", 1.0)) + m["summary"] * m["out"] + cold(m["post"], m)
    return c + (C * m["read"] if warm else cold(C, m))


def stretch_cost(s: dict, atom: Tuple[int, int, int], sp: Space, m: Dict[str, float], **kw) -> float:
    if s.get("free"):
        return 0.0
    p, c, cap = atom
    return cost(s["g"], s["C"], p, c, cap, sp.T, sp.lead, m, te=s.get("te", 0.0), switch=s.get("switch", False),
                manual=s.get("end") == "compaction", **kw)


def cost_rows(stretches: List[dict], sp: Space, m: Dict[str, float]) -> List[List[float]]:
    return [[stretch_cost(s, a, sp, m) for a in sp.atoms] for s in stretches]


def seg_totals(rows, segs, na: int, nseg: int, idx: Optional[Sequence[int]] = None) -> List[List[float]]:
    S = [[0.0] * na for _ in range(nseg)]
    add = operator.add
    for i in (range(len(rows)) if idx is None else idx):
        acc = S[segs[i]]
        acc[:] = map(add, acc, rows[i])
    return S


def rule_totals(S: List[List[float]], sp: Space) -> List[float]:
    return [sum(S[s][a] for s, a in enumerate(atoms)) for atoms in sp.rule_seg_atoms]


def nothing_total(S: List[List[float]], sp: Space) -> float:
    return sum(row[sp.nothing] for row in S)


def pick(totals: List[float], sp: Space) -> int:
    """Cheapest rule; exact ties go to the rule closest to the current one."""
    cA, cB, cN = sp.rules[sp.current]

    def key(i):
        A, B, N = sp.rules[i]
        return (round(totals[i], 3), i != sp.current, abs(N - cN), abs(A - cA), abs(min(B, 1e7) - min(cB, 1e7)))
    return min(range(len(totals)), key=key)


# ----------------------------------------------------------------------------- loading

def load_events(run: Path) -> List[dict]:
    out = []
    with open(run / "events.jsonl", "rb") as f:
        for raw in f:
            raw = raw.strip()
            if raw:
                try:
                    e = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(e, dict) and isinstance(e.get("t"), (int, float)):
                    out.append(e)
    out.sort(key=lambda e: e["t"])
    return out


def load_sleep(run: Path) -> Optional[Dict[str, Any]]:
    p = run / "sleep.json"
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return None
    iv = []
    for x in d.get("intervals") or []:
        try:
            a, b = float(x["start"]), float(x["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if b > a:
            iv.append((a, b, str(x.get("reason") or "")))
    if not iv:
        return None
    win = d.get("window")
    return {"intervals": sorted(iv), "window": win if isinstance(win, list) and len(win) == 2 else None}


def asleep_fn(iv: List[Tuple[float, float, str]]) -> Callable[[float], Optional[float]]:
    starts = [a for a, _, _ in iv]

    def f(t: float) -> Optional[float]:
        i = bisect.bisect_right(starts, t) - 1
        return iv[i][1] if i >= 0 and iv[i][0] <= t < iv[i][1] else None
    return f


# ----------------------------------------------------------------------------- Claude Code view

def session_ttl(resp: List[dict]) -> str:
    w1 = sum((e["usage"] or {}).get("cache_write_1h") or 0 for e in resp)
    w5 = sum((e["usage"] or {}).get("cache_write_5m") or 0 for e in resp)
    if w1 == 0 and w5 == 0:
        return "?"
    return "1h" if w1 >= w5 else "5m"


def cc_sessions(events: List[dict]) -> Dict[str, List[dict]]:
    S: Dict[str, List[dict]] = collections.defaultdict(list)
    for e in events:
        if e.get("tool") == CC and e.get("interactive") is not False and not e.get("subagent"):
            S[e["session"]].append(e)
    return S


def turn_state(between: List[dict], t0: float) -> Optional[float]:
    """Seconds after walk-away when the turn is known to have ended; None = still running at the return.

    A turn_end gives the time. Claude Code writes its turn_duration row for only about half of all turns, so a
    missing turn_end is not evidence of a running turn: a prompt or auto_prompt before the next request means the
    turn had ended (taken as at the walk-away). No turn_end and no prompt means the next request answered a tool
    result or a permission: the turn was running throughout.
    """
    for e in between:
        if e["kind"] == "turn_end":
            return max(0.0, e["t"] - t0)
    return 0.0 if any(e["kind"] in ("prompt", "auto_prompt") for e in between) else None


def return_label(between: List[dict], te: Optional[float], end_t: float, t0: float) -> str:
    if te is None or te > end_t - t0:
        return "within a turn"
    kinds = {e["kind"] for e in between}
    if "prompt" in kinds:
        return "human"
    if "auto_prompt" in kinds:
        return "automatic"
    return "other"


def build_cc(events: List[dict], ttl_opt: str, data_end: float, lead: float, current: Dict[str, Any]) -> Dict[str, Any]:
    sessions = cc_sessions(events)
    ttl_of, mix = {}, collections.Counter()
    for sid, ev in sessions.items():
        resp = [e for e in ev if e["kind"] == "response" and e.get("usage")]
        if not resp:
            continue
        ttl_of[sid] = session_ttl(resp)
        mix[ttl_of[sid]] += 1
    known = {t: n for t, n in mix.items() if t != "?"}
    dominant = max(known, key=known.get) if known else "1h"
    ttl = dominant if ttl_opt == "auto" else ttl_opt
    T = 3600.0 if ttl == "1h" else 300.0
    sp = Space(T, lead, current)
    thr = sp.threshold()

    stretches, vpairs, comp_post_ctx, comp_post_tokens, comp_pre, miss_reads = [], [], [], [], [], []
    counts = collections.Counter()
    sess_index: Dict[str, int] = {}
    for sid, ev in sessions.items():
        if sid not in ttl_of:
            continue
        own = ttl_of[sid] if ttl_of[sid] != "?" else dominant
        use = ttl_opt != "auto" or own == ttl
        if not use:
            counts["excluded_ttl_sessions"] += 1
        si = sess_index.setdefault(sid, len(sess_index))
        has_te = any(e["kind"] == "turn_end" for e in ev)
        counts["sessions_with_turn_end" if has_te else "sessions_without_turn_end"] += 1
        prev, between, pending_comp = None, [], False
        for e in ev:
            kd = e["kind"]
            if kd == "compaction":
                c = e.get("compaction") or {}
                if c.get("post"):
                    comp_post_tokens.append(c["post"])
                if c.get("pre"):
                    comp_pre.append(c["pre"])
                if prev is not None and use and e["t"] - prev["t"] >= thr:
                    # a /compact inside the gap: the person was there; the stretch ends at the compaction
                    te = turn_state(between, prev["t"])
                    stretches.append({"t0": prev["t"], "g": e["t"] - prev["t"], "C": float(prev["usage"]["ctx"]), "s": si,
                                      "te": te, "switch": False, "end": "compaction",
                                      "ret": return_label(between, te, e["t"], prev["t"])})
                prev, between, pending_comp = None, [], True     # the mod resets on any compaction
                continue
            if kd != "response" or not e.get("usage") or e["usage"].get("ctx") is None:
                between.append(e)
                continue
            u = e["usage"]
            if pending_comp:
                comp_post_ctx.append(u["ctx"])
                pending_comp = False
            if prev is not None and use:
                g = e["t"] - prev["t"]
                pctx = prev["usage"]["ctx"]
                same_model = e.get("model") == prev.get("model")
                if pctx >= 20_000 and same_model and u.get("cache_read") is not None:
                    miss = (u.get("cache_read") or 0) < 0.5 * pctx
                    vpairs.append((g, miss))
                    if miss and g >= T:
                        miss_reads.append(u.get("cache_read") or 0)
                if g >= thr:
                    te = turn_state(between, prev["t"])
                    stretches.append({"t0": prev["t"], "g": g, "C": float(pctx), "s": si, "te": te,
                                     
                                      "switch": not same_model, "end": "response",
                                      "ret": return_label(between, te, e["t"], prev["t"])})
            prev, between = e, []
        if prev is not None and use:
            if any(x["kind"] == "prompt" for x in between):
                counts["ended_by_command"] += 1        # e.g. /exit after the last response: the mod stops with the CLI
            elif data_end - prev["t"] > 4 * T:
                te = 0.0   # nothing after the last request: taken as between turns
                stretches.append({"t0": prev["t"], "g": None, "C": float(prev["usage"]["ctx"]), "s": si, "te": te,
                                  "switch": False, "end": "never", "ret": "never"})
            else:
                counts["censored"] += 1

    edges = [T / 12, T * 11 / 12, T, T * 13 / 12, 2 * T, INF]
    val = []
    for lo, hi in zip(edges, edges[1:]):
        xs = [mm for g, mm in vpairs if lo <= g < hi]
        val.append({"lo_s": lo, "hi_s": None if hi == INF else hi, "label": f"{dur(lo)}–{dur(hi)}",
                    "n": len(xs), "misses": sum(xs), "rate": (sum(xs) / len(xs)) if xs else None})
    return {"sessions": sessions, "ttl": ttl, "T": T, "space": sp, "ttl_mix": dict(mix), "dominant": dominant,
            "stretches": stretches, "validation": val, "counts": dict(counts), "n_sessions": len(ttl_of),
            "comp_post_ctx": comp_post_ctx, "comp_post_tokens": comp_post_tokens, "comp_pre": comp_pre,
            "miss_reads": miss_reads, "sess_index": sess_index}


def model_params(cc: Dict[str, Any], pricing: Dict[str, float]) -> Dict[str, Any]:
    flags = []
    post_ctx = med(cc["comp_post_ctx"])
    if post_ctx is None:
        post_ctx = DEFAULT_POST_CTX
        flags.append(f"no compactions in the history: post-compaction context defaults to {k(DEFAULT_POST_CTX)}")
    post_tok = med(cc["comp_post_tokens"])
    if post_tok is not None:
        summary = SUMMARY_PER_POST * post_tok
        flags.append(f"summary output estimated as {SUMMARY_PER_POST:g} × median postTokens ({k(post_tok)}); "
                     "events carry no summary length")
    else:
        summary = DEFAULT_SUMMARY_OUT
        flags.append(f"summary output not measurable: default {k(DEFAULT_SUMMARY_OUT)} tokens")
    W = med(cc["miss_reads"])
    if W is None:
        W = 0.0
        flags.append("no observed cold returns: the shared prefix still cached on a cold return is taken as 0")
    write = pricing["cache_write_1h"] if cc["ttl"] == "1h" else pricing["cache_write_5m"]
    return {"read": pricing["cache_read"], "write": write, "out": pricing["output"],
            "compact_read": pricing["cache_read"], "summary": float(summary), "post": float(post_ctx),
            "input": float(pricing.get("input", 1.0)),
            "ping_out": float(pricing.get("ping_output_tokens", 60)), "W": float(W), "W_n": len(cc["miss_reads"]),
            "flags": flags, "n_compactions": len(cc["comp_pre"]) or len(cc["comp_post_tokens"]),
            "pre_median": med(cc["comp_pre"]), "post_tokens_median": post_tok}


# ----------------------------------------------------------------------------- search and rigor

def search(stretches: List[dict], sp: Space, m: Dict[str, float], rows=None) -> Dict[str, Any]:
    rows = rows if rows is not None else cost_rows(stretches, sp, m)
    segs = [sp.seg_of(s["C"]) for s in stretches]
    S = seg_totals(rows, segs, len(sp.atoms), len(sp.bps))
    tot = rule_totals(S, sp)
    base = nothing_total(S, sp)
    return {"rows": rows, "segs": segs, "S": S, "totals": tot, "base": base, "best": pick(tot, sp)}


def merged_bands(stretches: List[dict]) -> Tuple[List[Tuple[float, float, List[int]]], bool]:
    bands, merged = [], False
    cur_lo, cur = BAND_EDGES[0], []
    for lo, hi in zip(BAND_EDGES, BAND_EDGES[1:]):
        cur += [i for i, s in enumerate(stretches) if lo <= s["C"] < hi]
        if len(cur) >= MIN_BAND_N:
            bands.append((cur_lo, hi, cur))
            cur_lo, cur = hi, []
        elif cur or bands:
            merged = True
    if cur:
        if bands:
            lo0, _, idx0 = bands.pop()
            bands.append((lo0, INF, idx0 + cur))
        else:
            bands.append((cur_lo, INF, cur))
    return bands, merged


def band_table(stretches, sp: Space, rows) -> Dict[str, Any]:
    bands, merged = merged_bands(stretches)
    cand = list(range(len(sp.atoms)))
    out, tot_best, tot_none = [], 0.0, 0.0
    for lo, hi, idx in bands:
        sums = {a: sum(rows[i][a] for i in idx) for a in cand}
        none = sums[sp.nothing]
        order = sorted(cand, key=lambda a: round(sums[a], 3))
        b = order[0]
        tot_best += sums[b]
        tot_none += none
        out.append({"lo": lo, "hi": None if hi == INF else hi, "label": f"{k(lo)}–{k(hi)}", "n": len(idx),
                    "best": atom_name(sp.atoms[b]), "best_saving": saving(sums[b], none),
                    "runners_up": [{"policy": atom_name(sp.atoms[a]), "saving": saving(sums[a], none)} for a in order[1:4]]})
    return {"bands": out, "merged_small_bands": merged, "per_band_optimum_saving": saving(tot_best, tot_none)}


def cluster_draws(stretches: List[dict], n_res: int, rnd: random.Random) -> List[Tuple[List[int], List[int]]]:
    """Resample sessions with replacement; each draw is (in-bag stretch indices, out-of-bag stretch indices)."""
    by: Dict[int, List[int]] = collections.defaultdict(list)
    for i, s in enumerate(stretches):
        by[s["s"]].append(i)
    keys = list(by)
    out = []
    for _ in range(n_res):
        idx: List[int] = []
        drawn = set()
        for _ in keys:
            k_ = keys[int(rnd.random() * len(keys))]
            drawn.add(k_)
            idx.extend(by[k_])
        out.append((idx, [i for k_ in keys if k_ not in drawn for i in by[k_]]))
    return out


def relation(lo: Optional[float], medn: Optional[float]) -> str:
    """How the best rule compares with the current one, from the out-of-sample gain (points)."""
    if lo is None or medn is None:
        return "untested"
    if lo > 0 and medn >= CHANGE_MIN_POINTS:
        return "better"
    if lo > 0:
        return "better, below the bar"
    return "equivalent"


def bootstrap(res: Dict[str, Any], sp: Space, draws, best: int) -> Dict[str, Any]:
    """Session-clustered bootstrap.

    Intervals: the best and the current rule scored on each resample. Verdict (optimism-corrected): in each
    resample the best rule is re-selected on the in-bag sessions and its gain over the current rule is measured
    on the out-of-bag sessions it was not fitted to — the gain that choosing a rule from data actually buys.
    """
    rows, segs = res["rows"], res["segs"]
    if not rows or not draws:
        return {"resamples": 0}
    cur, na, nseg = sp.current, len(sp.atoms), len(sp.bps)
    s_best, s_cur, gain, oob_gain, optimism, wins = [], [], [], [], [], collections.Counter()
    near_best = near_cur = 0
    for idx, oob in draws:
        S = seg_totals(rows, segs, na, nseg, idx)
        tot = rule_totals(S, sp)
        base = nothing_total(S, sp)
        if base <= 0:
            continue
        b = pick(tot, sp)
        wins[b] += 1
        sb, sc, sx = saving(tot[best], base), saving(tot[cur], base), saving(tot[b], base)
        s_best.append(sb)
        s_cur.append(sc)
        gain.append(100 * (sb - sc))
        near_best += (sx - sb) <= 0.01
        near_cur += (sx - sc) <= 0.01
        if oob:
            So = seg_totals(rows, segs, na, nseg, oob)
            bo = nothing_total(So, sp)
            if bo > 0:
                g_out = 100 * (So_rule(So, sp, cur) - So_rule(So, sp, b)) / bo
                oob_gain.append(g_out)
                optimism.append(100 * (sx - sc) - g_out)
    n = len(s_best) or 1
    return {"resamples": len(s_best), "clustered_by": "session",
            "lo": pct(s_best, 0.05), "hi": pct(s_best, 0.95), "median": pct(s_best, 0.5),
            "current_lo": pct(s_cur, 0.05), "current_hi": pct(s_cur, 0.95),
            "gain_lo": pct(gain, 0.05), "gain_hi": pct(gain, 0.95), "gain_median": pct(gain, 0.5),
            "oob_gain_lo": pct(oob_gain, 0.05), "oob_gain_hi": pct(oob_gain, 0.95), "oob_gain_median": pct(oob_gain, 0.5),
            "oob_resamples": len(oob_gain), "optimism_points": (sum(optimism) / len(optimism)) if optimism else None,
            "p_best_worse": sum(1 for x in gain if x < 0) / n,
            "chosen_wins": wins[best] / n, "current_wins": wins[cur] / n,
            "near_optimal_share": near_best / n, "current_near_optimal_share": near_cur / n,
            "top_winners": [{"rule": rule_name(sp.rules[i]), "share": c / n} for i, c in wins.most_common(5)]}


def So_rule(S: List[List[float]], sp: Space, i: int) -> float:
    return sum(S[s][a] for s, a in enumerate(sp.rule_seg_atoms[i]))


def verdict(best: int, sp: Space, boot: Dict[str, Any]) -> str:
    """Change only if the out-of-sample gain of re-selecting the best rule is above 0 (90%) with a median ≥ 1 point."""
    if best == sp.current:
        return "keep"
    return "change" if relation(boot.get("oob_gain_lo"), boot.get("oob_gain_median")) == "better" else "keep"


def holdout(stretches, sp: Space, m) -> Optional[Dict[str, Any]]:
    order = sorted(range(len(stretches)), key=lambda i: stretches[i]["t0"])
    if len(order) < 20:
        return None
    cut = int(0.7 * len(order))
    train = [stretches[i] for i in order[:cut]]
    test = [stretches[i] for i in order[cut:]]
    rt, re_ = search(train, sp, m), search(test, sp, m)
    ch = rt["best"]
    s_ch, s_opt = saving(re_["totals"][ch], re_["base"]), saving(re_["totals"][re_["best"]], re_["base"])
    return {"train_n": len(train), "test_n": len(test), "train_rule": rule_name(sp.rules[ch]),
            "train_saving": saving(rt["totals"][ch], rt["base"]), "test_saving_of_train_rule": s_ch,
            "test_current_saving": saving(re_["totals"][sp.current], re_["base"]),
            "test_optimum_rule": rule_name(sp.rules[re_["best"]]), "test_optimum_saving": s_opt,
            "gap_points": None if s_ch is None else 100 * (s_opt - s_ch),
            "split_at": time.strftime("%Y-%m-%d", time.gmtime(stretches[order[cut]]["t0"]))}


def sensitivity(stretches, sp: Space, m, best: int, rec: int, pricing, ttl: str, current) -> List[Dict[str, Any]]:
    other_write = pricing["cache_write_5m"] if ttl == "1h" else pricing["cache_write_1h"]
    variants: List[Tuple[str, Dict[str, Any], Optional[Callable[[dict], dict]], Optional[float]]] = [
        ("baseline", {}, None, None)]
    for L in SENS_LEADS:
        if L != sp.lead and L < sp.T / 60 / 2:
            variants.append((f"act {L:g} min early (the model cannot price a late ping; see method)", {}, None, L))
    variants += [
        ("compaction reads the context uncached (1.0×)", {"compact_read": pricing.get("input", 1.0)}, None, None),
        ("summary output ×2", {"summary": m["summary"] * 2}, None, None),
        ("post-compaction context ×2", {"post": m["post"] * 2}, None, None),
        ("ping output 200 tokens", {"ping_out": 200.0}, None, None),
        (f"cache write {other_write:g}× instead of {m['write']:g}×", {"write": other_write}, None, None),
        ("no shared prefix on cold returns (W = 0)", {"W": 0.0}, None, None),
        ("never-returned stretches cost nothing (CLI closed)", {}, lambda s: dict(s, free=s["g"] is None), None),
        ("a manual /compact return priced as an ordinary request", {},
         lambda s: dict(s, end="compaction-as-request") if s.get("end") == "compaction" else s, None),
        ("mid-turn ignored (compaction always allowed)", {}, lambda s: dict(s, te=0.0), None),
    ]
    out = []
    for label, ch, tf, L in variants:
        mm = dict(m)
        mm.update(ch)
        sp2 = Space(sp.T, L, current) if L is not None else sp
        st2 = [tf(s) for s in stretches] if tf else stretches
        r = search(st2, sp2, mm)
        out.append({"variant": label, "recommended_saving": saving(r["totals"][rec], r["base"]),
                    "best_rule_saving": saving(r["totals"][best], r["base"]),
                    "current_saving": saving(r["totals"][sp2.current], r["base"]),
                    "variant_best_rule": rule_name(sp2.rules[r["best"]]),
                    "variant_best_saving": saving(r["totals"][r["best"]], r["base"]),
                    "same_rule": r["best"] == best})
    return out


def return_table(stretches, T: float, thr: float) -> Dict[str, Any]:
    edges = [thr, T, 2 * T, 3 * T, 8 * T, INF]
    labels = [f"{dur(a)}–{dur(b)}" if b < INF else f"> {dur(a)}" for a, b in zip(edges, edges[1:])]
    labels[0] += " (still warm)"
    labels.append("never came back")
    rows = []
    for li, lab in enumerate(labels):
        if li < len(edges) - 1:
            lo, hi = edges[li], edges[li + 1]
            xs = [s for s in stretches if s["g"] is not None and lo <= s["g"] < hi]
        else:
            xs = [s for s in stretches if s["g"] is None]
        cells = [sum(1 for s in xs if a <= s["C"] < b) for a, b, _ in TABLE_CTX]
        kinds = collections.Counter(s["ret"] for s in xs)
        rows.append({"label": lab, "by_ctx": cells, "n": len(xs), **{r: kinds.get(r, 0) for r in RETURN_LABELS},
                     "manual_compact": sum(1 for s in xs if s.get("end") == "compaction")})
    n = len(stretches)
    return {"ctx_labels": [c for _, _, c in TABLE_CTX], "rows": rows, "n": n, "return_labels": list(RETURN_LABELS),
            "never_share": (sum(1 for s in stretches if s["g"] is None) / n) if n else None}


def gap_distribution(gaps: List[Optional[float]], T: float, thr: float) -> List[Dict[str, Any]]:
    edges = [thr, T, 2 * T, 3 * T, 8 * T, INF]
    out = []
    for a, b in zip(edges, edges[1:]):
        out.append({"label": f"{dur(a)}–{dur(b)}" if b < INF else f"> {dur(a)}",
                    "n": sum(1 for g in gaps if g is not None and a <= g < b)})
    out.append({"label": "never came back", "n": sum(1 for g in gaps if g is None)})
    return out


# ----------------------------------------------------------------------------- global view

def presence_index(events: List[dict]) -> Dict[str, Any]:
    """Other-tool presence (points and session intervals) and Claude Code human prompts by session."""
    pts, pfid = [], []
    sess_iv: Dict[Tuple[str, str], List[float]] = {}
    cc_prompts = []
    for e in events:
        if e.get("interactive") is False or e.get("subagent") or e.get("kind") not in PRESENCE_KINDS:
            continue
        if e["tool"] == CC:
            if e["kind"] == "prompt":
                cc_prompts.append((e["t"], e["session"]))
            continue
        if e.get("fidelity") == "session":
            iv = sess_iv.setdefault((e["tool"], e["session"]), [e["t"], e["t"]])
            iv[0], iv[1] = min(iv[0], e["t"]), max(iv[1], e["t"])
        else:
            pts.append(e["t"])
            pfid.append(e.get("fidelity") or "turn")
    ivs = sorted(tuple(v) for v in sess_iv.values())
    pmax, mx = [], -INF
    for a, b in ivs:
        mx = max(mx, b)
        pmax.append(mx)
    return {"pts": pts, "pfid": pfid, "iv_starts": [a for a, _ in ivs], "iv_pmax": pmax,
            "cc_t": [t for t, _ in cc_prompts], "cc_s": [s for _, s in cc_prompts]}


def other_tool_present(P, a: float, b: float) -> Optional[str]:
    i = bisect.bisect_right(P["pts"], a)
    j = bisect.bisect_right(P["pts"], b)
    if j > i:
        fids = set(P["pfid"][i:j])
        return "turn" if "turn" in fids else ("mtime" if "mtime" in fids else "turn")
    x = bisect.bisect_right(P["iv_starts"], b) - 1
    if x >= 0 and P["iv_pmax"][x] > a:
        return "session"
    return None


def other_cc_present(P, a: float, b: float, sid: str) -> bool:
    i = bisect.bisect_right(P["cc_t"], a)
    j = bisect.bisect_right(P["cc_t"], b)
    return any(P["cc_s"][x] != sid for x in range(i, j))


def class_search(stretches, sp, m, labels: List[str], blind_best: int, base: float) -> Dict[str, Any]:
    classes = {}
    aware = 0.0
    for lab in sorted(set(labels)):
        xs = [s for s, l in zip(stretches, labels) if l == lab]
        r = search(xs, sp, m)
        aware += r["totals"][r["best"]]
        classes[lab] = {"n": len(xs), "best_rule": rule_name(sp.rules[r["best"]]),
                        "best_saving": saving(r["totals"][r["best"]], r["base"]),
                        "blind_rule_saving": saving(r["totals"][blind_best], r["base"]),
                        "returns": gap_distribution([s["g"] for s in xs], sp.T, sp.threshold()),
                        "median_ctx": med([s["C"] for s in xs])}
    blind = search(stretches, sp, m)["totals"][blind_best]
    return {"classes": classes, "aware_saving": saving(aware, base), "blind_saving": saving(blind, base),
            "value_points": None if base <= 0 else 100 * (blind - aware) / base}


def pooled_gaps(events: List[dict], T: float, thr: float, data_end: float) -> Dict[str, Any]:
    """Every tool: idle gaps that end in a human `prompt` (prompt − the session's previous event), plus
    never-returned sessions. Returns (tool, gap, session key) triples."""
    by_sess: Dict[Tuple[str, str], List[dict]] = collections.defaultdict(list)
    for e in events:
        if e.get("interactive") is False or e.get("subagent"):
            continue
        by_sess[(e["tool"], e["session"])].append(e)
    rows: List[Tuple[str, Optional[float], int]] = []
    no_turns = collections.Counter()
    for si, ((tool, _), ev) in enumerate(by_sess.items()):
        if not any(e["kind"] == "prompt" for e in ev):
            no_turns[tool] += 1
            continue
        for a, b in zip(ev, ev[1:]):
            if b["kind"] == "prompt" and b["t"] - a["t"] >= thr:
                rows.append((tool, b["t"] - a["t"], si))
        if data_end - ev[-1]["t"] > 4 * T:
            rows.append((tool, None, si))
    return {"rows": rows, "sessions_without_prompts": dict(no_turns)}


def gap_breakpoints(sp: Space) -> List[float]:
    step = sp.T - sp.lead * 60
    acts = [step * j for j in range(1, 8)]
    return sorted(set(acts + [sp.T] + [a + sp.T for a in acts]))


def global_search(ctx_st: List[dict], gap_rows: List[Tuple[str, Optional[float], int]], sp: Space, m,
                  rnd: random.Random, n_res: int) -> Optional[Dict[str, Any]]:
    """Pooled return times × Claude Code contexts at walk-away, assumed independent; returns are taken as
    between turns. Cost is affine in C on each side of the shared prefix W given the gap class, so totals
    need only per-segment sums of C and counts."""
    if not ctx_st or not gap_rows:
        return None
    na, W = len(sp.atoms), m.get("W", 0.0)
    bps = gap_breakpoints(sp)
    cls: Dict[Any, int] = {}
    reps: List[Optional[float]] = []
    gcls = []
    for _, g, _ in gap_rows:
        key = "never" if g is None else bisect.bisect_left(bps, g)
        if key not in cls:
            cls[key] = len(reps)
            reps.append(g)
        gcls.append(cls[key])
    hi_c = 2 * W + 1e5

    def piece(g, a, c0, c1):
        y0 = stretch_cost({"g": g, "C": c0}, a, sp, m)
        y1 = stretch_cost({"g": g, "C": c1}, a, sp, m)
        slope = (y1 - y0) / (c1 - c0)
        return slope, y0 - slope * c0
    vec = []          # per class: [(slope_lo, int_lo, slope_hi, int_hi) per atom]
    for g in reps:
        vec.append([piece(g, a, 0.0, W) + piece(g, a, W, hi_c) if W > 0 else piece(g, a, 0.0, 1e5) * 2
                    for a in sp.atoms])
    csegs = [sp.seg_of(s["C"]) for s in ctx_st]
    cside = [1 if s["C"] >= W else 0 for s in ctx_st]
    nseg = len(sp.bps)

    def totals(gcount: List[int], cidx: Sequence[int]):
        ng = sum(gcount)
        coef = [[0.0] * 4 for _ in range(na)]
        for j, n in enumerate(gcount):
            if n:
                for a in range(na):
                    v = vec[j][a]
                    ca = coef[a]
                    for q in range(4):
                        ca[q] += n * v[q]
        coef = [[x / ng for x in ca] for ca in coef]
        sumC = [[0.0, 0.0] for _ in range(nseg)]
        cnt = [[0, 0] for _ in range(nseg)]
        for i in cidx:
            s_, d = csegs[i], cside[i]
            sumC[s_][d] += ctx_st[i]["C"]
            cnt[s_][d] += 1
        S = [[coef[a][0] * sumC[s_][0] + coef[a][1] * cnt[s_][0] + coef[a][2] * sumC[s_][1] + coef[a][3] * cnt[s_][1]
              for a in range(na)] for s_ in range(nseg)]
        return rule_totals(S, sp), nothing_total(S, sp)

    full = [0] * len(reps)
    for c in gcls:
        full[c] += 1
    tot, base = totals(full, range(len(ctx_st)))
    best = pick(tot, sp)
    # session-clustered resamples of both the gaps and the contexts
    gby: Dict[int, List[int]] = collections.defaultdict(list)
    for i, (_, _, si) in enumerate(gap_rows):
        gby[si].append(i)
    cby: Dict[int, List[int]] = collections.defaultdict(list)
    for i, s in enumerate(ctx_st):
        cby[s["s"]].append(i)
    gk, ck = list(gby), list(cby)
    sav, sav_cur, gain, oob_gain, wins = [], [], [], [], collections.Counter()
    for _ in range(n_res):
        gc, go = [0] * len(reps), [0] * len(reps)
        picks = [gk[int(rnd.random() * len(gk))] for _ in gk]
        for k_ in picks:
            for i in gby[k_]:
                gc[gcls[i]] += 1
        for k_ in set(gk) - set(picks):
            for i in gby[k_]:
                go[gcls[i]] += 1
        cpicks = [ck[int(rnd.random() * len(ck))] for _ in ck]
        cidx = [i for k_ in cpicks for i in cby[k_]]
        coob = [i for k_ in set(ck) - set(cpicks) for i in cby[k_]]
        if not sum(gc) or not cidx:
            continue
        t2, b2 = totals(gc, cidx)
        if b2 <= 0:
            continue
        bb = pick(t2, sp)
        wins[bb] += 1
        sb, sc = saving(t2[best], b2), saving(t2[sp.current], b2)
        sav.append(sb)
        sav_cur.append(sc)
        gain.append(100 * (sb - sc))
        if sum(go) and coob:
            t3, b3 = totals(go, coob)
            if b3 > 0:
                oob_gain.append(100 * (t3[sp.current] - t3[bb]) / b3)
    n = len(sav) or 1
    return {"best": best, "totals": tot, "base": base,
            "boot": {"resamples": len(sav), "clustered_by": "session", "lo": pct(sav, 0.05), "hi": pct(sav, 0.95),
                     "median": pct(sav, 0.5), "current_lo": pct(sav_cur, 0.05), "current_hi": pct(sav_cur, 0.95),
                     "gain_lo": pct(gain, 0.05), "gain_hi": pct(gain, 0.95), "gain_median": pct(gain, 0.5),
                     "oob_gain_lo": pct(oob_gain, 0.05), "oob_gain_hi": pct(oob_gain, 0.95),
                     "oob_gain_median": pct(oob_gain, 0.5), "oob_resamples": len(oob_gain),
                     "chosen_wins": wins[best] / n, "current_wins": wins[sp.current] / n,
                     "top_winners": [{"rule": rule_name(sp.rules[i]), "share": c / n} for i, c in wins.most_common(5)]}}


def cache_economics(events: List[dict]) -> List[Dict[str, Any]]:
    agg: Dict[str, Dict[str, Any]] = {}
    for e in events:
        u = e.get("usage")
        if e["kind"] != "response" or not u:
            continue
        a = agg.setdefault(e["tool"], {"responses": 0, "input": 0, "cache_read": 0, "cache_write": 0, "output": 0,
                                       "with_cache_fields": 0, "ctx": []})
        a["responses"] += 1
        for f in ("input", "cache_read", "cache_write", "output"):
            a[f] += u.get(f) or 0
        a["with_cache_fields"] += u.get("cache_read") is not None
        if u.get("ctx"):
            a["ctx"].append(u["ctx"])
    out = []
    for tool, a in sorted(agg.items()):
        prompt_side = a["input"] + a["cache_read"] + a["cache_write"]
        ok = prompt_side and a["with_cache_fields"]
        out.append({"tool": tool, "responses": a["responses"],
                    "cache_read_share": (a["cache_read"] / prompt_side) if ok else None,
                    "cache_write_share": (a["cache_write"] / prompt_side) if ok else None,
                    "median_ctx": med(a["ctx"]), "output_tokens": a["output"]})
    return out


# ----------------------------------------------------------------------------- recommendation

def mod_by_ttl(events: List[dict], data_end: float, lead: float, current: Dict[str, Any],
               pricing: Dict[str, float]) -> Dict[str, Dict[str, Any]]:
    """For the TTL comparison: the mod's saving per session (vs doing nothing) under each TTL, for the current rule
    and the rule that is best under that TTL, every interactive session counted whatever TTL it ran at."""
    out: Dict[str, Dict[str, Any]] = {}
    for t in ("5m", "1h"):
        c = build_cc(events, t, data_end, lead, current)
        sp, st = c["space"], c["stretches"]
        m = model_params(c, pricing)
        res = search(st, sp, m)
        inv = {v: kk for kk, v in c["sess_index"].items()}
        out[t] = {}
        n_cur = sp.rules[sp.current][2]
        for which, idx in (("current", sp.current), ("best", res["best"]), ("pings", None)):
            per: Dict[str, float] = collections.defaultdict(float)
            rule = sp.rules[idx] if idx is not None else None
            for i, s_ in enumerate(st):
                row = res["rows"][i]
                atom = rule_atom(rule, s_["C"]) if rule else (n_cur, 0, 0)   # /cache-clock pings: never compacts
                per[inv[s_["s"]]] += row[sp.ai[atom]] - row[sp.nothing]
            out[t][which] = {"rule": rule_name(rule, sp.lead) if rule else
                             f"≤{n_cur} ping{'s' if n_cur != 1 else ''} at any size, never compact · act {sp.lead:g} min early",
                             "per_session": dict(per), "n": len(st)}
    return out


def day_of(t: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d")


def load_spend(run: Path, events: List[dict]) -> Tuple[List[dict], bool]:
    """spend.json from collect.py; without it, only what events.jsonl kept (no subagents, no scripted runs)."""
    p = run / "spend.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8")).get("rows", []), True
    led: Dict[Any, Dict[str, int]] = {}
    for e in events:
        spend_add(led, e)
    return spend_rows(led), False


def weigh(row: Dict[str, Any], p: Dict[str, Any], ttl: str) -> float:
    """Input-token equivalents. A cache write the store did not split by TTL is priced at the analysed TTL."""
    other = p["cache_write_1h"] if ttl == "1h" else p["cache_write_5m"]
    return (row["input"] * p.get("input", 1.0) + row["output"] * p["output"] + row["cache_read"] * p["cache_read"]
            + row["cache_write_5m"] * p["cache_write_5m"] + row["cache_write_1h"] * p["cache_write_1h"]
            + row["cache_write_other"] * other)


def spend_ladder(rows: List[dict], complete: bool, window: Tuple[str, str], pricing: Dict[str, Any],
                 per_tool: Dict[str, Dict[str, float]], ttl: str, base: float, rec: Dict[str, Any]) -> Dict[str, Any]:
    """The saving as a share of ever-wider denominators. share(D) = share(gap cost) × gap cost / D, so each step
    shrinks the headline by the ratio of the two denominators; the steps say what each one adds."""
    agg: Dict[Tuple[str, str], Dict[str, float]] = collections.defaultdict(lambda: collections.defaultdict(float))
    for r in rows:
        if not window[0] <= r["day"] <= window[1]:
            continue
        a = agg[(r["tool"], r["class"])]
        a["cost"] += weigh(r, {**pricing, **per_tool.get(r["tool"], {})}, ttl)
        a["tokens"] += sum(r[f] for f in SPEND_FIELDS)
        a["responses"] += r["responses"]
        a["with_usage"] += r["with_usage"]

    def total(pred) -> float:
        return sum(a["cost"] for (t, c), a in agg.items() if pred(t, c))

    sv = rec.get("saving") or 0.0
    saved = base * sv
    lo, hi = (rec.get("interval") or [None, None])[:2]
    others = sorted({t for t, _ in agg} - {CC})
    priced_others = [t for t in others if any(agg[(t, c)]["with_usage"] for c in ("main", "subagent", "headless"))]
    spec = [("idle-gap cost", "what the returns after your idle stretches cost with no mod (modelled: the do-nothing "
             "row above)", None, True),
            ("Claude Code, interactive, main thread", "every main-conversation request in the window",
             lambda t, c: t == CC and c == "main", True),
            ("+ subagents", "the subagents those conversations started (own cache prefixes; the mod cannot help them)",
             lambda t, c: t == CC and c in ("main", "subagent"), complete),
            ("+ scripted runs (claude -p, SDK)", "headless runs and their subagents (the mod is inert there)",
             lambda t, c: t == CC, complete),
            ("+ other AI tools", ("tools with token counts: " + (", ".join(tool_name(t) for t in priced_others) or "none"))
             + ("; weighted with Claude's price ratios unless pricing.json per_tool says otherwise" if priced_others else ""),
             lambda t, c: True, complete and bool(priced_others))]
    steps, prev = [], None
    for label, what, pred, ok in spec:
        if not ok:
            steps.append({"step": label, "what": what, "collected": False})
            continue
        den = base if pred is None else total(pred)
        steps.append({"step": label, "what": what, "collected": True, "denominator": den,
                      "gap_share": (base / den) if den else None, "saving_share": (saved / den) if den else None,
                      "interval": [None if x is None or not den else x * base / den for x in (lo, hi)],
                      "factor": (prev / den) if prev and den else None})
        prev = den
    per = []
    for t in [CC] + others:
        cls = {c: agg[(t, c)] for c in ("main", "subagent", "headless") if (t, c) in agg}
        if not cls:
            continue
        resp = sum(a["responses"] for a in cls.values())
        withu = sum(a["with_usage"] for a in cls.values())
        per.append({"tool": t, "cost": {c: a["cost"] for c, a in cls.items()},
                    "total": sum(a["cost"] for a in cls.values()), "tokens": sum(a["tokens"] for a in cls.values()),
                    "responses": int(resp), "responses_without_counts": int(resp - withu)})
    return {"saved": saved, "saving_on_gap": sv, "window": list(window), "complete": complete, "steps": steps,
            "per_tool": per, "uncounted_tools": [t for t in others if t not in priced_others]}


def confidence(n: int, lo, hi, gap, near) -> str:
    width = None if lo is None or hi is None else 100 * (hi - lo)
    if n < 50 or width is None or width > 25 or (gap is not None and gap > 10):
        return "low"
    if n >= 200 and width <= 12 and (gap is None or gap <= 3) and (near or 0) >= 0.7:
        return "high"
    return "medium"


def settings(rule, ttl: str, lead: float, act: bool = True) -> Dict[str, Any]:
    A, B, N = rule
    return {"ttl": ttl, "autoAct": act, "leadMinutes": lead, "keepAliveBelowTokens": int(A), "maxKeepAlives": N,
            "compactAboveTokens": int(B) if B < INF else 1_000_000_000}


def recommend(sp: Space, best: int, tot, base, boot, n, hold_gap, ttl, current_src: str, when: str) -> Dict[str, Any]:
    v = verdict(best, sp, boot)
    cur_s, best_s = saving(tot[sp.current], base), saving(tot[best], base)
    if best_s is None or best_s <= 0:
        return {"verdict": "warn-only", "rule": "do nothing (warn-only)",
                "settings": settings(sp.rules[sp.current], ttl, sp.lead, act=False), "saving": 0.0,
                "interval": [None, None], "n": n, "confidence": confidence(n, None, None, hold_gap, 0), "when": when,
                "current_source": current_src}
    chosen = best if v == "change" else sp.current
    lo, hi = (boot.get("lo"), boot.get("hi")) if v == "change" else (boot.get("current_lo"), boot.get("current_hi"))
    near = boot.get("near_optimal_share") if v == "change" else boot.get("current_near_optimal_share")
    out = {"verdict": v, "rule": rule_name(sp.rules[chosen], sp.lead), "settings": settings(sp.rules[chosen], ttl, sp.lead),
           "saving": saving(tot[chosen], base), "interval": [lo, hi], "n": n,
           "confidence": confidence(n, lo, hi, hold_gap, near), "when": when, "current_source": current_src,
           "current_rule": rule_name(sp.rules[sp.current], sp.lead), "current_saving": cur_s}
    if best != chosen or v == "change":
        out["alternative"] = {"rule": rule_name(sp.rules[best], sp.lead), "settings": settings(sp.rules[best], ttl, sp.lead),
                              "saving": best_s, "gain_points": 100 * (best_s - cur_s),
                              "gain_interval": [boot.get("gain_lo"), boot.get("gain_hi")],
                              "gain_median": boot.get("gain_median"),
                              "oob_gain_interval": [boot.get("oob_gain_lo"), boot.get("oob_gain_hi")],
                              "oob_gain_median": boot.get("oob_gain_median"),
                              "relation": relation(boot.get("oob_gain_lo"), boot.get("oob_gain_median"))}
    return out


# ----------------------------------------------------------------------------- main

def clean(x):
    if isinstance(x, float):
        return None if (math.isinf(x) or math.isnan(x)) else round(x, 6)
    if isinstance(x, dict):
        return {str(kk): clean(v) for kk, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [clean(v) for v in x]
    return x


def sleep_section(slp, st, sp: Space, m, rec_rule, data_end: float, generated: Optional[float]) -> Dict[str, Any]:
    iv_all = slp["intervals"]
    lo_w = float(slp["window"][0]) if slp["window"] else iv_all[0][0]
    hi_w = float(slp["window"][1]) if slp["window"] else (generated or data_end)
    hi_w = min(hi_w, data_end)
    sure = [x for x in iv_all if "(to DarkWake)" not in x[2]]
    inwin = [s for s in st if lo_w <= s["t0"] and s["t0"] + sp.threshold() <= hi_w]

    def run(iv):
        f = asleep_fn(iv) if iv else None
        c_wake = c_sleep = c_none = 0.0
        affected = 0
        for s in inwin:
            a = rule_atom(rec_rule, s["C"])
            x1 = stretch_cost(s, a, sp, m)
            x2 = stretch_cost(s, a, sp, m, asleep=f, t0=s["t0"]) if f else x1
            c_wake += x1
            c_sleep += x2
            c_none += stretch_cost(s, (0, 0, 0), sp, m)
            affected += abs(x1 - x2) > 1e-9
        return {"affected": affected, "affected_share": (affected / len(inwin)) if inwin else None,
                "saving_ignoring_sleep": saving(c_wake, c_none), "saving_with_sleep": saving(c_sleep, c_none)}
    main_ = run(sure)
    main_.update({"available": True, "window": [lo_w, hi_w],
                  "window_source": "sleep.json window" if slp["window"] else "first sleep record → collection time",
                  "intervals": len(sure), "darkwake_intervals": len(iv_all) - len(sure),
                  "stretches_in_window": len(inwin),
                  "asleep_hours_in_window": sum(min(b, hi_w) - max(a, lo_w) for a, b, _ in sure if b > lo_w and a < hi_w) / 3600,
                  "with_darkwake": run(iv_all) if len(iv_all) != len(sure) else None})
    return main_


def analyze(run: Path, pricing_path: Path = DEFAULT_PRICING, ttl_opt: str = "auto", seed: int = 7,
            n_res: int = 1000, lead: Optional[float] = None, settings_path: Optional[Path] = None) -> Dict[str, Any]:
    t_start = time.time()
    rnd = random.Random(seed)
    pricing = {kk: v for kk, v in json.loads(Path(pricing_path).read_text(encoding="utf-8")).items() if not kk.startswith("_")}
    tool_prices = pricing.pop("per_tool", None) or {}
    current, cur_src = read_current(settings_path)
    lead_src = "--lead" if lead is not None else ("your settings" if cur_src == "your settings" else "mod default")
    lead = float(lead if lead is not None else current["lead"])
    events = load_events(run)
    cov_path = run / "coverage.json"
    coverage = json.loads(cov_path.read_text(encoding="utf-8")) if cov_path.exists() else {"tools": []}
    data_end = max((e["t"] for e in events), default=0.0)
    cc = build_cc(events, ttl_opt, data_end, lead, current)
    sp, T, ttl = cc["space"], cc["T"], cc["ttl"]
    thr = sp.threshold()
    m = model_params(cc, pricing)
    st = cc["stretches"]
    warnings = []

    # ---- Claude Code view
    res = search(st, sp, m)
    best, tot, base, rows = res["best"], res["totals"], res["base"], res["rows"]
    draws = cluster_draws(st, n_res, rnd)
    boot = bootstrap(res, sp, draws, best)
    hold = holdout(st, sp, m)
    when = ("Fitted to your own Claude Code context sizes and return times. A change is recommended only when the "
            "best rule, re-selected in each session-clustered resample and scored on the sessions left out, gains "
            "more than 0 (90%) with a median of at least 1 point.")
    rec_cc = recommend(sp, best, tot, base, boot, len(st), hold["gap_points"] if hold else None, ttl, cur_src, when)
    rec_idx = best if rec_cc.get("verdict") == "change" else sp.current

    ai = sp.ai
    N_cur = sp.rules[sp.current][2]
    named = [("do nothing", lambda s: (0, 0, 0)), ("always 1 ping", lambda s: (1, 0, 0)),
             ("always compact", lambda s: (0, 1, N_cur))]
    policies = [{"policy": nm, "total": sum(rows[i][ai[f(s)]] for i, s in enumerate(st)), "kind": "reference"}
                for nm, f in named]
    policies.append({"policy": f"your current rule ({cur_src}): " + rule_name(sp.rules[sp.current], sp.lead),
                     "total": tot[sp.current], "kind": "current"})
    bt = band_table(st, sp, rows)
    policies.append({"policy": "per-band optimum (not a simple rule)",
                     "total": None if bt["per_band_optimum_saving"] is None else base * (1 - bt["per_band_optimum_saving"]),
                     "kind": "bound"})
    policies.append({"policy": "best 3-tier rule: " + rule_name(sp.rules[best], sp.lead), "total": tot[best], "kind": "best"})
    for p in policies:
        p["saving"] = None if p["total"] is None else saving(p["total"], base)
    top_rules = sorted(range(len(tot)), key=lambda i: tot[i])[:8]
    sens = sensitivity(st, sp, m, best, rec_idx, pricing, ttl, current)
    rec_cc["assumption_range"] = assumption_range(sens)
    if len(st) < 100:
        warnings.append(f"only {len(st)} idle stretches: the chosen thresholds are weakly determined")
    for b in bt["bands"]:
        if b["n"] < MIN_BAND_N:
            warnings.append(f"context band {b['label']} has n={b['n']} < {MIN_BAND_N} even after merging")
    if bt["merged_small_bands"]:
        warnings.append(f"context bands with fewer than {MIN_BAND_N} stretches were merged with a neighbour")

    # mid-turn at the first action
    first_act = T - sp.lead * 60
    midturn_first = sum(1 for s in st if s["te"] is None or s["te"] > first_act)

    # ---- sleep
    sleep_out: Dict[str, Any] = {"available": False}
    slp = load_sleep(run)
    if slp:
        from datetime import datetime
        gen = coverage.get("generated")
        try:
            gen_t = datetime.fromisoformat(gen.replace("Z", "+00:00")).timestamp() if gen else None
        except ValueError:
            gen_t = None
        sleep_out = sleep_section(slp, st, sp, m, sp.rules[rec_idx], data_end, gen_t)

    # ---- global view: presence
    P = presence_index(events)
    lab_other, lab_any, fid = [], [], collections.Counter()
    inv_sess = {v: kk for kk, v in cc["sess_index"].items()}
    for s in st:
        a, b = s["t0"], s["t0"] + first_act
        o = other_tool_present(P, a, b)
        lab_other.append("elsewhere" if o else "away")
        if o:
            fid[o] += 1
        lab_any.append("elsewhere" if (o or other_cc_present(P, a, b, inv_sess[s["s"]])) else "away")
    presence = {"window": f"walk-away → first action ({dur(first_act)})",
                "other_tools": class_search(st, sp, m, lab_other, rec_idx, base) if st else None,
                "elsewhere_evidence_fidelity": dict(fid),
                "any_other_session": class_search(st, sp, m, lab_any, rec_idx, base) if st else None}

    # ---- global view: pooled behaviour
    pg = pooled_gaps(events, T, thr, data_end)
    gap_rows = pg["rows"]
    per_tool = collections.defaultdict(list)
    for tool, g, _ in gap_rows:
        per_tool[tool].append(g)
    tools_with_gaps = sorted(per_tool)
    other_tools = [t for t in tools_with_gaps if t != CC]
    n_pool = len(gap_rows)
    mix = {t: len(v) / n_pool for t, v in per_tool.items()} if n_pool else {}
    mix_label = "global (" + ", ".join(f"{100 * s:.0f}% {tool_name(t)}" for t, s in
                                       sorted(mix.items(), key=lambda kv: -kv[1])) + ")" if mix else "global"
    gs = global_search(st, gap_rows, sp, m, rnd, n_res)
    glob: Dict[str, Any] = {"tools_with_turn_data": tools_with_gaps, "other_tools_with_turn_data": other_tools,
                            "only_claude_code": not other_tools, "pooled_n": n_pool, "mix": mix, "label": mix_label,
                            "pooled_dist": gap_distribution([g for _, g, _ in gap_rows], T, thr),
                            "per_tool": {t: {"n": len(v), "dist": gap_distribution(v, T, thr)} for t, v in per_tool.items()},
                            "sessions_without_prompts": pg["sessions_without_prompts"],
                            "never_share_pooled": (sum(1 for _, g, _ in gap_rows if g is None) / n_pool) if n_pool else None}
    if gs:
        gb = gs["best"]
        g_when = ("Return times pooled over every tool with per-prompt timestamps, paired with your Claude Code "
                  "contexts (assumed independent). A fallback when Claude Code history is thin — not independent "
                  "evidence: " + (f"{100 * mix.get(CC, 0):.0f}% of its returns are Claude Code's own."))
        rec_g = recommend(sp, gb, gs["totals"], gs["base"], gs["boot"], n_pool, None, ttl, cur_src, g_when)
        if rec_g["confidence"] == "high":     # the independence assumption is untested: cap it
            rec_g["confidence"] = "medium"
        g_idx = gb if rec_g.get("verdict") == "change" else sp.current
        rec_g.update({"label": mix_label, "contexts_n": len(st), "model_saving": rec_g["saving"],
                      "saving_on_claude_code_history": saving(tot[g_idx], base)})
        glob.update({"rule": rule_name(sp.rules[gb], sp.lead), "saving_model": saving(gs["totals"][gb], gs["base"]),
                     "boot": gs["boot"], "same_rule_as_claude_code": gb == best,
                     "same_recommendation": g_idx == rec_idx,
                     "saving_on_claude_code_history": saving(tot[gb], base),
                     "claude_code_rule_saving": saving(tot[best], base),
                     "points_vs_claude_code_rule": 100 * (tot[best] - tot[gb]) / base if base else None})
    else:
        rec_g = None

    # ---- out of what: the saving as a share of total spend, over the window of the Claude Code timeline
    cc_t = [e["t"] for e in events if e["tool"] == CC and e["kind"] == "response" and e.get("interactive") is not False
            and not e.get("subagent")]
    spend_rows_, spend_complete = load_spend(run, events)
    spend = spend_ladder(spend_rows_, spend_complete, (day_of(min(cc_t)), day_of(max(cc_t))) if cc_t else ("", ""),
                         pricing, tool_prices, ttl, base, rec_cc) if cc_t and base else None
    if spend and not spend_complete:
        warnings.append("no spend.json (collect.py writes it): the share of spend covers only the main thread")

    # ---- which TTL: 5 minutes or 1 hour, per kind of traffic, the mod layered on the interactive lane
    ttl_rows = ttl_mod.load(run)
    if ttl_rows:
        ttl_choice = ttl_mod.compare(ttl_rows, pricing, m["W"], mod_by_ttl(events, data_end, lead, current, pricing),
                                     n_res, rnd)
    else:
        ttl_choice = {"available": False}
        warnings.append("no ttl.jsonl (collect.py writes it): the 5-minute vs 1-hour comparison was skipped")

    # ---- coverage
    tools_cov = []
    for t in coverage.get("tools", []):
        row = {kk: t.get(kk) for kk in ("tool", "status", "verified_adapter", "sessions", "interactive_sessions",
                                        "headless_sessions", "first", "last", "per_month_sessions", "events",
                                        "events_kept", "fidelity", "kinds", "parse_errors")}
        if row.get("status") == "ok" and not (row.get("events") or row.get("sessions")):
            row["status"] = "empty"
        tools_cov.append(row)

    out = {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "seed": seed, "resamples": n_res,
        "pricing": pricing, "ttl": {"used": ttl, "T_seconds": T, "override": ttl_opt != "auto", "mix": cc["ttl_mix"],
                                    "excluded_sessions_other_ttl": cc["counts"].get("excluded_ttl_sessions", 0)},
        "lead": {"used": sp.lead, "source": lead_src},
        "current": {"source": cur_src, "rule": rule_name(sp.rules[sp.current], sp.lead),
                    "settings": settings(sp.rules[sp.current], ttl, sp.lead)},
        "validation": cc["validation"],
        "claude_code": {
            "sessions": cc["n_sessions"], "stretches": len(st), "threshold_s": thr, "lead": sp.lead,
            "n_rules": len(sp.rules),
            "counts": cc["counts"], "returns": dict(collections.Counter(s["ret"] for s in st)),
            "manual_compact_returns": sum(1 for s in st if s["end"] == "compaction"),
            "model_switch_returns": sum(1 for s in st if s["switch"]),
            "midturn_at_first_action": midturn_first,
            "ctx_at_walkaway": {"median": med([s["C"] for s in st]), "p75": pct([s["C"] for s in st], 0.75)},
            "return_table": return_table(st, T, thr), "policies": policies, "base_total": base,
            "top_rules": [{"rule": rule_name(sp.rules[i]), "saving": saving(tot[i], base)} for i in top_rules],
            "bands": bt, "bootstrap": boot, "holdout": hold, "sensitivity": sens,
            "best_rule": rule_name(sp.rules[best], sp.lead), "best_saving": saving(tot[best], base),
            "current_rule": rule_name(sp.rules[sp.current], sp.lead), "current_saving": saving(tot[sp.current], base),
        },
        "model": dict(m),
        "spend": spend, "ttl_choice": ttl_choice, "sleep": sleep_out, "presence": presence, "global": glob, "cache_economics": cache_economics(events),
        "coverage": {"generated": coverage.get("generated"), "system": coverage.get("system"), "tools": tools_cov,
                     "power": coverage.get("power")},
        "recommendation": {"claude_code": rec_cc, **({"global": rec_g} if rec_g else {})},
        "warnings": warnings, "seconds": round(time.time() - t_start, 1),
    }
    out["summary_lines"] = summary_lines(out)
    return clean(out)


def assumption_range(sens: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Spread of the recommended rule's saving over the modelling-assumption rows (lead rows and the
    'compaction uncached' row reported separately)."""
    rows = [x for x in sens if x["variant"] != "baseline" and not x["variant"].startswith("act ")
            and "uncached" not in x["variant"] and x["recommended_saving"] is not None]
    unc = next((x["recommended_saving"] for x in sens if "uncached" in x["variant"]), None)
    if not rows:
        return {"lo": None, "hi": None, "uncached": unc}
    lo = min(rows, key=lambda x: x["recommended_saving"])
    hi = max(rows, key=lambda x: x["recommended_saving"])
    return {"lo": lo["recommended_saving"], "lo_variant": lo["variant"], "hi": hi["recommended_saving"],
            "hi_variant": hi["variant"], "uncached": unc}


def range_line(ar: Optional[Dict[str, Any]]) -> str:
    if not ar or ar.get("lo") is None:
        return ""
    s = f"assumption rows span {fmt_pct(ar['lo'])}–{fmt_pct(ar['hi'])}"
    if ar.get("uncached") is not None:
        s += f"; {fmt_pct(ar['uncached'])} if compaction cannot read the cache"
    return s


def alt_line(alt: Dict[str, Any]) -> str:
    """N1: 'equivalent' only when the out-of-sample gain interval includes 0."""
    gi, gm = alt.get("oob_gain_interval") or [None, None], alt.get("oob_gain_median")
    ivs = f" (out of sample, 90% {gi[0]:+.1f} to {gi[1]:+.1f})" if gi[0] is not None else ""
    if alt.get("relation") == "better, below the bar":
        return f"Better by ~{gm:.1f} pts{ivs}, below the 1-point bar for changing: {short_rule(alt['rule'])}"
    if alt.get("relation") == "equivalent":
        return f"Equivalent: {short_rule(alt['rule'])}, {gm:+.1f} pts{ivs}"
    return f"Best rule {short_rule(alt['rule'])}: {alt['gain_points']:+.1f} pts in sample"


def fmt_pct(x) -> str:
    return "n/a" if x is None else f"{100 * x:.0f}%"


def fmt_share(x) -> str:
    """Small shares keep a decimal: 2.5%, not 3%."""
    return "n/a" if x is None else (f"{100 * x:.1f}%" if abs(x) < 0.1 else f"{100 * x:.0f}%")


def short_rule(rule: str) -> str:
    import re
    rule = re.sub(r" · act [0-9.]+ min early$", "", rule)
    return rule.replace("1 ping then compact", "ping→compact").replace(": ", " ")


def spend_tail(r: Dict[str, Any]) -> str:
    """' = x% of <widest collected denominator>' — the headline never goes out without the total-spend share."""
    sp = r.get("spend") or {}
    done = [s for s in sp.get("steps", [])[1:] if s.get("collected") and s.get("saving_share") is not None]
    if not done:
        return ""
    s = done[-1]
    what = {"Claude Code, interactive, main thread": "your interactive Claude Code main-thread spend",
            "+ subagents": "your interactive Claude Code spend", "+ scripted runs (claude -p, SDK)": "all Claude Code spend",
            "+ other AI tools": "all AI-tool spend counted"}[s["step"]]
    return f" = {fmt_share(s['saving_share'])} of {what}"


def summary_lines(r: Dict[str, Any]) -> List[str]:
    """At most five short lines: the headline of every report."""
    c = r["recommendation"]["claude_code"]
    iv = c["interval"]
    ivs = f" (90% {fmt_pct(iv[0])}–{fmt_pct(iv[1])})" if iv[0] is not None else ""
    alt = c.get("alternative")
    if c["verdict"] == "change":
        lines = [f"Change your rule: saves {fmt_pct(c['saving'])}{ivs} of idle-gap cost vs {fmt_pct(c['current_saving'])} "
                 f"now{spend_tail(r)} · "
                 f"n={c['n']} · sampling confidence {c['confidence']}",
                 f"New rule: {short_rule(c['rule'])}"]
    elif c["verdict"] == "keep":
        lines = [f"Keep your current rule ({c['current_source']}): saves {fmt_pct(c['saving'])}{ivs} of idle-gap cost"
                 f"{spend_tail(r)} · n={c['n']} · sampling confidence {c['confidence']}"]
        if alt:
            lines.append(alt_line(alt))
        else:
            lines.append("Your current rule is also the best simple rule")
    else:
        lines = ["No rule beats doing nothing: switch cache-clock to warn-only", ""]
    tc = r.get("ttl_choice") or {}
    lines.append(ttl_mod.short(tc) if tc.get("available") else "TTL " + r["ttl"]["used"] + " misses: " + miss_line(r["validation"]))
    g = r["recommendation"].get("global")
    if g:
        same = "same verdict" if r["global"].get("same_recommendation") else \
            f"{g['verdict']} → {short_rule(g['rule'])} ({fmt_pct(g['saving_on_claude_code_history'])} on CC history)"
        lines.append(f"{r['global']['label'].capitalize()[0] + r['global']['label'][1:]}: {same}")
    else:
        lines.append("Global: no pooled return data")
    tail = [range_line(c.get("assumption_range"))]
    s = r["sleep"]
    tail.append(f"sleep blocks an action in {fmt_pct(s['affected_share'])} of stretches" if s.get("available") else "no sleep log")
    t = " · ".join(tail)
    lines.append(t[0].upper() + t[1:])
    return [x for x in lines if x][:5]


def miss_line(val) -> str:
    """'<55m 1% · 60–65m 70% · >65m 99%' from the validation buckets (0 = short, 2 = just past, 3+ = later)."""
    def rate(vs):
        n = sum(v["n"] for v in vs)
        return f"{100 * sum(v['misses'] for v in vs) / n:.0f}%" if n else "n/a"
    if len(val) < 4:
        return "n/a"
    def mm(sec):
        return dur(sec).replace(" ", "")
    return (f"<{mm(val[0]['hi_s'])} {rate(val[:1])} · {mm(val[2]['lo_s'])}–{mm(val[2]['hi_s'])} {rate(val[2:3])} · "
            f">{mm(val[3]['lo_s'])} {rate(val[3:])}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", required=True)
    ap.add_argument("--pricing", default=str(DEFAULT_PRICING))
    ap.add_argument("--ttl", default="auto", choices=["auto", "5m", "1h"])
    ap.add_argument("--lead", type=float, default=None,
                    help="minutes before expiry the mod acts (default: your cache-clock setting, else 3)")
    ap.add_argument("--settings", default=None, help="Claude Code settings.json to read cache-clock options from")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--resamples", type=int, default=1000)
    a = ap.parse_args(argv)
    run = Path(a.run)
    if not (run / "events.jsonl").exists():
        print("no events.jsonl in the run folder — run collect.py first", file=sys.stderr)
        return 2
    r = analyze(run, Path(a.pricing), a.ttl, a.seed, max(1, a.resamples), a.lead,
                Path(a.settings) if a.settings else None)
    (run / "results.json").write_text(json.dumps(r, indent=1, ensure_ascii=False), encoding="utf-8")
    print("\n".join(r["summary_lines"]))
    print(f"[{r['seconds']} s] results.json written")
    return 0


if __name__ == "__main__":
    from cps_common import utf8_console
    utf8_console()
    sys.exit(main())
