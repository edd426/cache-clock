"""Which prompt-cache TTL: 5 minutes or 1 hour, per kind of Claude Code traffic.

Claude Code sets the TTL separately for the main conversation (`promptCacheTtl`; the environment variable
CLAUDE_CODE_PROMPT_CACHE_TTL wins, so a scripted run can set its own) and for everything else — subagents,
workflows, helpers (`subagentPromptCacheTtl`). A 1-hour cache write costs 2x the input price instead of 1.25x;
in return a request that comes back 5-60 minutes after the last one reads the cache (0.1x) instead of writing it
again. Whether that pays depends only on how the person's requests are spaced, which ttl.jsonl records.

The model (method.md, "Which TTL"), per lane — one conversation, or one subagent — request by request:
  * first request of a lane, after a compaction or a model switch, or less than 5 minutes after the previous
    one: both TTLs read what was observed (the TTL makes no difference there), and write the rest;
  * 5-60 minutes after the previous request: under 5 minutes the entry has lapsed — only the shared prefix W
    (system prompt and tools, kept warm by other sessions) is read; under 1 hour the entry is alive and the
    survivable prefix is read. Where the history itself ran at that TTL the observed read is used, so only the
    other TTL is modelled: a 1-hour read is f x min(previous, current cacheable context), f measured from the
    person's own 1-hour returns when there are enough, else 0.8 (0.5 and 1.0 are shown as scenarios);
  * over 60 minutes: both lapse — W is read, the rest written (the mod's keep-alive is added separately).
Everything is in input-token equivalents. The cache-clock mod is layered on the interactive lane only (it is
inert in scripted runs): its saving per idle stretch, from analyze.py's own cost model under each TTL.
"""
from __future__ import annotations

import bisect
import collections
import json
import random
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

FIVE, HOUR = 300.0, 3600.0
LANES = ("main", "headless", "subagent")
LANE_NAMES = {"main": "interactive conversations", "headless": "scripted runs (claude -p, SDK)",
              "subagent": "subagents"}   # helper and side requests follow the subagent setting too, but leave no transcript
SETTING = {"main": '"promptCacheTtl"', "headless": "CLAUDE_CODE_PROMPT_CACHE_TTL in the run's environment",
           "subagent": '"subagentPromptCacheTtl" (or CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL)'}
F_DEFAULT = 0.8
F_SCENARIOS = (0.5, 0.8, 1.0)
F_MIN_PAIRS = 30
MIN_POINTS = 1.0               # recommend a switch only when it saves at least 1% of the lane's spend …
POLICY_BREAK_EVEN = (2.0 - 1.25) / (1.25 - 0.10)   # … the 65.2% reuse ratio, for the pricing sheet's ratios


def load(run: Path) -> Optional[List[dict]]:
    p = run / "ttl.jsonl"
    if not p.exists():
        return None
    rows = []
    with open(p, "rb") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                e = json.loads(raw)
            except ValueError:
                continue
            if isinstance(e, dict) and isinstance(e.get("t"), (int, float)) and e.get("class") in LANES:
                rows.append(e)
    rows.sort(key=lambda e: e["t"])
    return rows


def wrote(u: dict) -> Optional[str]:
    w5, w1 = u.get("cache_write_5m") or 0, u.get("cache_write_1h") or 0
    if w1 > w5:
        return "1h"
    if w5 > 0:
        return "5m"
    return None


def lanes_of(rows: List[dict]) -> Dict[str, Dict[str, List[dict]]]:
    out: Dict[str, Dict[str, List[dict]]] = {c: collections.defaultdict(list) for c in LANES}
    for e in rows:
        out[e["class"]][e["lane"]].append(e)
    return out


def walk(lane: List[dict]):
    """Yield (event, gap seconds or None, reusable prefix S, cold-start flag) for each response with token counts."""
    prev, reset = None, True
    for e in lane:
        if e["kind"] == "compaction":
            reset = True
            continue
        u = e.get("usage")
        if e["kind"] != "response" or not u or u.get("cache_read") is None:
            continue
        cacheable = (u.get("cache_read") or 0) + (u.get("cache_write") or 0)
        if prev is None or reset or e.get("model") != prev[1]:
            yield e, None, 0.0, cacheable
        else:
            yield e, e["t"] - prev[0], min(prev[2], cacheable), cacheable
        prev, reset = (e["t"], e.get("model"), cacheable), False


def survival(lanes: Dict[str, List[dict]]) -> Tuple[Optional[float], int]:
    """Token-weighted share of the reusable prefix a 1-hour cache actually served on 5-60 minute returns."""
    num = den = 0.0
    n = 0
    for lane in lanes.values():
        lt = lane_ttl(lane)
        for e, g, S, _ in walk(lane):
            if g is None or not (FIVE < g <= HOUR) or S <= 0 or (wrote(e["usage"]) or lt) != "1h":
                continue
            num += min(e["usage"].get("cache_read") or 0, S)
            den += S
            n += 1
    return (num / den if den else None), n


def lane_ttl(lane: List[dict]) -> Optional[str]:
    w = collections.Counter()
    for e in lane:
        u = e.get("usage") or {}
        w["5m"] += u.get("cache_write_5m") or 0
        w["1h"] += u.get("cache_write_1h") or 0
    if not (w["5m"] or w["1h"]):
        return None
    return "1h" if w["1h"] >= w["5m"] else "5m"


def price(u: dict, read: float, write: float, wmult: float, p: Dict[str, float]) -> float:
    return ((u.get("input") or 0) * p.get("input", 1.0) + (u.get("output") or 0) * p["output"]
            + read * p["cache_read"] + write * wmult)


def neighbours(lanes: Dict[str, List[dict]]) -> Dict[Any, List[float]]:
    """Request times per model across every lane of a class: a new lane's system prompt and tools can still be
    cached when another lane of the same kind sent them within the TTL."""
    idx: Dict[Any, List[float]] = collections.defaultdict(list)
    for lane in lanes.values():
        for e in lane:
            if e["kind"] == "response" and e.get("usage"):
                idx[e.get("model")].append(e["t"])
    for v in idx.values():
        v.sort()
    return idx


def since_other(idx: Dict[Any, List[float]], model, t: float) -> float:
    ts = idx.get(model) or []
    i = bisect.bisect_left(ts, t) - 1
    return t - ts[i] if i >= 0 else float("inf")


def shared_prefix(lanes: Dict[str, List[dict]], idx, W: float) -> Tuple[float, int]:
    """Median cache read on a lane's first request when another lane ran within 5 minutes: what one conversation
    of this kind reuses from another (system prompt, tools). Falls back to analyze.py's W."""
    xs = []
    for lane in lanes.values():
        for e, g, _, _ in walk(lane):
            if g is None:
                if since_other(idx, e.get("model"), e["t"]) <= FIVE:
                    xs.append(float(e["usage"].get("cache_read") or 0))
                break
    xs.sort()
    return (xs[len(xs) // 2], len(xs)) if len(xs) >= 10 else (W, len(xs))


def lane_costs(lanes: Dict[str, List[dict]], p: Dict[str, float], W: float, f: float,
               trust_obs: bool = True) -> Dict[str, Any]:
    """Per session: modelled cost under each TTL, observed cost, and the 5-60 minute opportunity.

    Where the history ran at a TTL its observed read is used for that TTL; only the other TTL is modelled. A
    lane's own entry gone (5 minutes under 5m, over an hour under both), the request reads the shared prefix Wc
    only if another lane of the same kind and model sent a request within that TTL, else nothing.
    trust_obs=False applies the model's formulas to both TTLs, observed reads ignored outside the under-5-minute
    band: its cost at the observed TTL against the observed cost is the model check that can fail."""
    idx = neighbours(lanes)
    Wc, _ = shared_prefix(lanes, idx, W)
    per: Dict[str, Dict[str, float]] = collections.defaultdict(lambda: collections.Counter())
    tot = collections.Counter()
    gaps = collections.Counter()
    for lid, lane in lanes.items():
        obs_ttl = lane_ttl(lane)
        acc = per[lid.split("/")[0]]
        for e, g, S, cacheable in walk(lane):
            u = e["usage"]
            R = float(u.get("cache_read") or 0)
            obs = wrote(u) or obs_ttl
            first = g is None
            ng = since_other(idx, e.get("model"), e["t"])
            if not first and g <= FIVE:
                r5 = r1 = R
                band = "under 5 min"
            elif not first and g <= HOUR:
                if trust_obs:
                    r1 = R if obs == "1h" else max(R, min(cacheable, f * S))
                    r5 = R if obs == "5m" else (min(R, Wc) if ng <= FIVE else 0.0)
                else:
                    r1 = min(cacheable, f * S)
                    r5 = min(R, Wc) if ng <= FIVE else 0.0
                if obs != "1h":
                    tot["f_modelled"] += 1
                band = "5-60 min"
            else:
                band = "first" if first else "over 60 min"
                if obs != "1h" and trust_obs:   # observed at 5m (or wrote nothing): what happened is the 5m
                    r5 = r1 = R                 # outcome and a lower bound for 1h
                else:
                    cap = R if first else min(R, Wc)   # past an hour a warm read may be the mod's keep-alive: cap it
                    r1 = R if (first and trust_obs) else (cap if ng <= HOUR else 0.0)
                    r5 = cap if ng <= FIVE else 0.0
            c5 = price(u, r5, cacheable - r5, p["cache_write_5m"], p)
            c1 = price(u, r1, cacheable - r1, p["cache_write_1h"], p)
            w5, w1 = u.get("cache_write_5m") or 0, u.get("cache_write_1h") or 0
            other = max(0, (u.get("cache_write") or 0) - w5 - w1)
            cobs = (price(u, R, 0.0, 0.0, p) + w5 * p["cache_write_5m"] + w1 * p["cache_write_1h"]
                    + other * (p["cache_write_1h"] if obs_ttl == "1h" else p["cache_write_5m"]))
            for key, v in (("5m", c5), ("1h", c1), ("observed", cobs)):
                acc[key] += v
                tot[key] += v
            tot["requests"] += 1
            tot["w1h_policy"] += cacheable - r1
            tot["read_gain_1h"] += r1 - r5
            gaps[band] += 1
    return {"per_session": per, "totals": tot, "gaps": gaps, "Wc": Wc}


def boot_delta(per: Dict[str, Dict[str, float]], a: Callable[[Dict[str, float]], float],
               b: Callable[[Dict[str, float]], float], n_res: int, rnd: random.Random) -> Tuple[float, float, float]:
    """Session-clustered bootstrap of (cost a - cost b) / cost b. Returns (point, 5th, 95th percentile)."""
    keys = list(per)
    va = [a(per[s]) for s in keys]
    vb = [b(per[s]) for s in keys]
    point = (sum(va) - sum(vb)) / sum(vb) if sum(vb) else 0.0
    if len(keys) < 2:
        return point, point, point
    xs = []
    n = len(keys)
    for _ in range(n_res):
        idx = [rnd.randrange(n) for _ in range(n)]
        sa, sb = sum(va[i] for i in idx), sum(vb[i] for i in idx)
        if sb:
            xs.append((sa - sb) / sb)
    xs.sort()
    return point, xs[int(0.05 * (len(xs) - 1))], xs[int(0.95 * (len(xs) - 1))]


def verdict(point: float, lo: float, hi: float, current: Optional[str]) -> str:
    """Positive = 1 hour costs more than 5 minutes. A switch needs the 90% interval clear of zero and >= 1 point."""
    if lo > 0 and 100 * point >= MIN_POINTS:
        best = "5m"
    elif hi < 0 and -100 * point >= MIN_POINTS:
        best = "1h"
    else:
        return "either"
    return best if current != best else "keep " + best


def compare(rows: List[dict], pricing: Dict[str, float], W: float,
            mod: Optional[Dict[str, Dict[str, Any]]], n_res: int, rnd: random.Random) -> Dict[str, Any]:
    """mod: {"5m"|"1h": {"current"|"best"|"pings": {"rule": str, "per_session": {sid: saving vs no mod}, "n": int}}}
    for the interactive lane, from analyze.py; None when the mod is not modelled."""
    by_class = lanes_of(rows)
    f_meas, f_n = survival({**by_class["main"], **by_class["headless"], **by_class["subagent"]})
    f = f_meas if (f_meas is not None and f_n >= F_MIN_PAIRS) else F_DEFAULT
    out: Dict[str, Any] = {"available": True, "f": f, "f_measured": f_meas, "f_pairs": f_n,
                           "f_source": "measured on your 1-hour returns" if f == f_meas else "default (too few 1-hour returns to measure)",
                           "W": W, "break_even": POLICY_BREAK_EVEN, "lanes": {}}
    for c in LANES:
        lanes = by_class[c]
        if not lanes:
            continue
        lc = lane_costs(lanes, pricing, W, f)
        per, T = lc["per_session"], lc["totals"]
        w = collections.Counter()
        for lane in lanes.values():
            for e in lane:
                u = e.get("usage") or {}
                w["5m"] += u.get("cache_write_5m") or 0
                w["1h"] += u.get("cache_write_1h") or 0
        current = None if not (w["5m"] or w["1h"]) else ("1h" if w["1h"] >= w["5m"] else "5m")
        row: Dict[str, Any] = {
            "name": LANE_NAMES[c], "setting": SETTING[c], "sessions": len(per), "lanes": len(lanes),
            "requests": T["requests"], "observed_ttl": current,
            "observed_writes": {"5m": w["5m"], "1h": w["1h"]},
            "gaps": dict(lc["gaps"]),
            "eligible_share": (lc["gaps"]["5-60 min"] / T["requests"]) if T["requests"] else None,
            "reuse_ratio": (T["read_gain_1h"] / T["w1h_policy"]) if T["w1h_policy"] else None,
            "shared_prefix": lc["Wc"],
            "observed_cost": T["observed"],
        }
        pure = lane_costs(lanes, pricing, W, f, trust_obs=False)["totals"]
        row["model_check"] = (pure[current] / T["observed"] - 1) if current and T["observed"] else None
        options = [{"ttl": t, "mod": "none", "rule": None, "total": T[t]} for t in ("5m", "1h")]
        mods = mod if (c == "main" and mod) else None
        if mods:
            for t in ("5m", "1h"):
                for which in ("current", "best", "pings"):
                    d = mods[t][which]
                    options.append({"ttl": t, "mod": which, "rule": d["rule"],
                                    "total": T[t] + sum(d["per_session"].values())})
        base = T[current] if current else T["5m"]
        for o in options:
            o["vs_now"] = (o["total"] / base - 1) if base else None

        def cost_of(t: str, which: str) -> Callable[[Dict[str, float]], float]:
            def fn(acc_sid):
                acc, sid = acc_sid
                v = acc[t]
                if which != "none":
                    v += mods[t][which]["per_session"].get(sid, 0.0)
                return v
            return fn
        which = "best" if mods else "none"
        keyed = {sid: (acc, sid) for sid, acc in per.items()}
        pt, lo, hi = boot_delta(keyed, cost_of("1h", which), cost_of("5m", which), n_res, rnd)
        row.update({"options": options, "compared": "with cache-clock's best rule for each TTL" if mods else "no mod",
                    "delta_1h_vs_5m": {"point": pt, "lo": lo, "hi": hi}, "verdict": verdict(pt, lo, hi, current)})
        if mods:
            p0, l0, h0 = boot_delta(keyed, cost_of("1h", "none"), cost_of("5m", "none"), n_res, rnd)
            row["delta_without_mod"] = {"point": p0, "lo": l0, "hi": h0, "verdict": verdict(p0, l0, h0, current)}
        m5 = sum(mods["5m"][which]["per_session"].values()) if mods else 0.0
        m1 = sum(mods["1h"][which]["per_session"].values()) if mods else 0.0
        scen = []
        if T["f_modelled"]:          # f only enters where a 5-60 minute return ran at 5 minutes
            for fs in F_SCENARIOS:
                lt = lane_costs(lanes, pricing, W, fs)["totals"]
                d5 = lt["5m"] + m5
                scen.append({"f": fs, "delta": (lt["1h"] + m1) / d5 - 1 if d5 else None})
            if row["verdict"] != "either" and any(
                    x["delta"] is None or (x["delta"] > 0) != (pt > 0) or 100 * abs(x["delta"]) < MIN_POINTS for x in scen):
                row["verdict"] = "either"
                row["verdict_note"] = "depends on prefix survival f"
        row["scenarios"] = scen
        out["lanes"][c] = row
    out["summary"] = summary(out)
    return out


def pctx(x: Optional[float]) -> str:
    return "n/a" if x is None else f"{100 * x:.0f}%" if abs(x) >= 0.095 else f"{100 * x:.1f}%"


def signed(x: Optional[float]) -> str:
    return "n/a" if x is None else ("+" if x >= 0 else "−") + pctx(abs(x))


def lane_line(c: str, r: Dict[str, Any]) -> str:
    """'Scripted: switch to 5m (1h vs 5m +21%, 90% +18% to +24%)' — the sign says which TTL is dearer."""
    d = r["delta_1h_vs_5m"]
    v = r["verdict"]
    name = {"main": "Interactive", "headless": "Scripted", "subagent": "Subagents"}[c]
    head = "either TTL" if v == "either" else (v if v.startswith("keep") else "switch to " + v)
    return f"{name}: {head} (1h vs 5m {signed(d['point'])}, 90% {signed(d['lo'])} to {signed(d['hi'])})"


def summary(out: Dict[str, Any]) -> str:
    parts = [lane_line(c, out["lanes"][c]) for c in LANES if c in out["lanes"]]
    return "TTL · " + " · ".join(parts) if parts else "TTL: no Claude Code requests with token counts"


def short(out: Dict[str, Any]) -> str:
    """One summary line: 'TTL · interactive keep 1h (1h −13% vs 5m) · scripted switch to 5m (+7.9%) · …'."""
    names = {"main": "interactive", "headless": "scripted", "subagent": "subagents"}
    parts = []
    for c in LANES:
        r = out["lanes"].get(c)
        if not r:
            continue
        v = r["verdict"]
        head = "either" if v == "either" else (v if v.startswith("keep") else "switch to " + v)
        parts.append(f"{names[c]} {head} ({signed(r['delta_1h_vs_5m']['point'])})")
    return "TTL, 1h vs 5m cost: " + " · ".join(parts) if parts else "TTL: no Claude Code requests with token counts"
