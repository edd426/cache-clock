#!/usr/bin/env python3
"""Render results.json as report.html (self-contained), summary.md and photo.html (one screen, no identifiers).

    python3 report.py --run DIR
"""
from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


def pc(x: Optional[float], digits: int = 0) -> str:
    if x is None:
        return "n/a"
    v = 100 * x
    return f"{0.0 if abs(v) < 0.5 * 10 ** -digits else v:.{digits}f}%"


def kt(x: Optional[float]) -> str:
    if x is None:
        return "n/a"
    return f"{x / 1e9:.2f}B" if x >= 1e9 else (f"{x / 1e6:.1f}M" if x >= 1e6 else f"{x / 1e3:.0f}k")


def share(x: Optional[float]) -> str:
    """Small shares keep a decimal: 2.5%, not 3%."""
    return pc(x, 1 if x is not None and abs(x) < 0.1 else 0)


CLASS_NAMES = (("main", "main thread"), ("subagent", "subagents"), ("headless", "scripted runs"))


def spend_rows_view(sp: Dict[str, Any]) -> List[Tuple[Any, ...]]:
    rows = []
    for st in sp["steps"]:
        if not st["collected"]:
            rows.append((st["step"], st["what"], "not collected", "", "", ""))
            continue
        lo, hi = st["interval"]
        iv = f" ({share(lo)}–{share(hi)})" if lo is not None else ""
        rows.append((st["step"], st["what"], kt(st["denominator"]), share(st["gap_share"]),
                     share(st["saving_share"]) + iv, "" if st["factor"] is None else f"× {st['factor']:.2f}"))
    return rows


def spend_lead(sp: Dict[str, Any]) -> str:
    return (f"The mod saved {kt(sp['saved'])} input-token equivalents ({sp['window'][0]} – {sp['window'][1]}): "
            f"{share(sp['saving_on_gap'])} of the idle-gap cost. The same saving divided by wider totals gives the "
            "smaller shares below. Each share is the row above × the ratio of the two totals (last column), i.e. "
            f"{share(sp['saving_on_gap'])} × gap cost ÷ that total.")


def esc(s: Any) -> str:
    return html.escape(str(s))


def interval(rec: Dict[str, Any]) -> str:
    lo, hi = rec.get("interval") or [None, None]
    return "" if lo is None else f"{pc(lo)}–{pc(hi)}"


# ----------------------------------------------------------------------------- charts (inline SVG)

def hbars(items: List[Tuple[str, Optional[float], str]], fmt=pc, width=720, vmin=None, vmax=None) -> str:
    """Horizontal bars; items = (label, value, css class). Values may be negative."""
    vals = [v for _, v, _ in items if v is not None]
    if not vals:
        return "<p class=muted>No data.</p>"
    lo = min(0.0, min(vals)) if vmin is None else vmin
    hi = max(0.0, max(vals)) if vmax is None else vmax
    hi = hi if hi > lo else lo + 1
    lab_w, val_w, row_h = 300, 60, 26
    plot = width - lab_w - val_w
    x = lambda v: lab_w + (v - lo) / (hi - lo) * plot
    h = row_h * len(items) + 8
    out = [f'<svg class=chart viewBox="0 0 {width} {h}" role="img" preserveAspectRatio="xMinYMin meet">']
    out.append(f'<line class=axis x1="{x(0):.1f}" x2="{x(0):.1f}" y1="0" y2="{h - 4}"/>')
    for i, (lab, v, cls) in enumerate(items):
        y = 4 + i * row_h
        short = lab if len(lab) <= 44 else lab[:42] + "…"
        out.append(f'<g class="row"><title>{esc(lab)}: {esc(fmt(v))}</title>'
                   f'<rect class=hit x="0" y="{y}" width="{width}" height="{row_h}"/>'
                   f'<text class=lab x="{lab_w - 8}" y="{y + 17}" text-anchor="end">{esc(short)}</text>')
        if v is not None:
            a, b = sorted((x(0), x(v)))
            out.append(f'<rect class="bar {cls}" x="{a:.1f}" y="{y + 5}" width="{max(1.5, b - a):.1f}" height="{row_h - 10}" rx="3"/>')
            out.append(f'<text class=val x="{max(x(0), x(v)) + 6:.1f}" y="{y + 17}">{esc(fmt(v))}</text>')
        out.append("</g>")
    out.append("</svg>")
    return "".join(out)


def stacked(rows: List[Tuple[str, Sequence[int]]], series: Sequence[str], width=720) -> str:
    """Horizontal stacked counts, one sequential step per series (the last 'never' series is gray), HTML legend above."""
    tot = max((sum(v) for _, v in rows), default=0) or 1
    lab_w, val_w, row_h = 200, 50, 26
    plot = width - lab_w - val_w
    cls = [f"seq{min(j, 4)}" if not s.startswith("never") else "seqn" for j, s in enumerate(series)]
    legend = "<div class=legend>" + "".join(f'<span><svg class=sw viewBox="0 0 10 10"><rect class="{c}" width="10" height="10" rx="2"/></svg>'
                                            f'{esc(s)}</span>' for c, s in zip(cls, series)) + "</div>"
    h = row_h * len(rows) + 6
    out = [legend, f'<svg class=chart viewBox="0 0 {width} {h}" role="img" preserveAspectRatio="xMinYMin meet">']
    for i, (lab, vals) in enumerate(rows):
        y = 2 + i * row_h
        out.append(f'<text class=lab x="{lab_w - 8}" y="{y + 17}" text-anchor="end">{esc(lab)}</text>')
        x = float(lab_w)
        for j, v in enumerate(vals):
            if not v:
                continue
            w = v / tot * plot
            out.append(f'<g class=row><title>{esc(lab)} · {esc(series[j])}: {v}</title>'
                       f'<rect class="{cls[j]}" x="{x:.1f}" y="{y + 5}" width="{max(1.0, w - 2):.1f}" height="{row_h - 10}" rx="2"/></g>')
            x += w
        out.append(f'<text class=val x="{x + 6:.1f}" y="{y + 17}">{sum(vals)}</text>')
    out.append("</svg>")
    return "".join(out)


def months_spark(per_month: Dict[str, int]) -> str:
    if not per_month:
        return ""
    items = sorted(per_month.items())
    mx = max(v for _, v in items) or 1
    w, h, bw = 8 * len(items) + 2, 22, 6
    bars = "".join(f'<rect class="bar best" x="{1 + 8 * i}" y="{h - 1 - 20 * v / mx:.1f}" width="{bw}" '
                   f'height="{max(1, 20 * v / mx):.1f}" rx="1"><title>{esc(m)}: {v}</title></rect>'
                   for i, (m, v) in enumerate(items))
    return f'<svg class=spark width="{w}" height="{h}" viewBox="0 0 {w} {h}">{bars}</svg>'


def table(head: Sequence[str], rows: Sequence[Sequence[Any]], num_from: int = 1) -> str:
    th = "".join(f"<th{' class=num' if i >= num_from else ''}>{esc(h)}</th>" for i, h in enumerate(head))
    body = "".join("<tr>" + "".join(f"<td{' class=num' if i >= num_from else ''}>{c if isinstance(c, Raw) else esc(c)}</td>"
                                    for i, c in enumerate(r)) + "</tr>" for r in rows)
    return f"<div class=tw><table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table></div>"


class Raw(str):
    pass


# ----------------------------------------------------------------------------- page

CSS = """
:root{color-scheme:light;--bg:#fcfcfb;--card:#ffffff;--ink:#0b0b0b;--ink2:#52514e;--muted:#7a7974;--line:#e4e3df;
--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;--q0:#cde2fb;--q1:#86b6ef;--q2:#3987e5;--q3:#1c5cab;--q4:#0d366b;--warn:#b25e00}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;--bg:#1a1a19;--card:#222221;--ink:#ffffff;
--ink2:#c3c2b7;--muted:#9a9990;--line:#383835;--s1:#3987e5;--s2:#d95926;--s3:#199e70;--q0:#184f95;--q1:#256abf;--q2:#3987e5;--q3:#6da7ec;--q4:#b7d3f6;--warn:#fab219}}
:root[data-theme="dark"]{color-scheme:dark;--bg:#1a1a19;--card:#222221;--ink:#ffffff;--ink2:#c3c2b7;--muted:#9a9990;--line:#383835;
--s1:#3987e5;--s2:#d95926;--s3:#199e70;--q0:#184f95;--q1:#256abf;--q2:#3987e5;--q3:#6da7ec;--q4:#b7d3f6;--warn:#fab219}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:980px;margin:0 auto;padding:24px 16px 64px}h1{font-size:24px;margin:0 0 12px}h2{font-size:18px;margin:36px 0 10px;
border-top:1px solid var(--line);padding-top:20px}h3{font-size:15px;margin:18px 0 6px}
.lead{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 18px;margin:0 0 16px}
.lead li{margin:2px 0}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.big{font-size:30px;font-weight:650}.muted{color:var(--muted)}.ink2{color:var(--ink2)}.warn{color:var(--warn)}
code,pre{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:13px}pre{background:var(--bg);border:1px solid var(--line);
border-radius:6px;padding:8px;overflow-x:auto;margin:8px 0 0}
.tw{overflow-x:auto}table{border-collapse:collapse;width:100%;margin:6px 0;font-size:14px}th,td{padding:5px 8px;border-bottom:1px solid var(--line);
text-align:left;vertical-align:top}th{color:var(--ink2);font-weight:600}.num{text-align:right;font-variant-numeric:tabular-nums}
svg.chart{width:100%;height:auto;display:block;margin:6px 0}svg text{fill:var(--ink2);font-size:13px}svg .val{fill:var(--ink);font-variant-numeric:tabular-nums}
svg .axis{stroke:var(--line);stroke-width:1}svg .hit{fill:transparent}svg .row:hover .hit{fill:var(--line);opacity:.4}
.bar{fill:var(--s1)}.bar.ref{fill:var(--muted)}.bar.cur{fill:var(--s2)}.bar.best{fill:var(--s1)}.bar.bound{fill:var(--s3)}
.seqn{fill:var(--muted)}.seq0{fill:var(--q0)}.seq1{fill:var(--q1)}.seq2{fill:var(--q2)}.seq3{fill:var(--q3)}.seq4{fill:var(--q4)}
svg.spark{vertical-align:middle}.tag{display:inline-block;border:1px solid var(--line);border-radius:999px;padding:0 8px;font-size:12px;color:var(--ink2)}
.legend{display:flex;flex-wrap:wrap;gap:4px 16px;font-size:13px;color:var(--ink2);margin:6px 0 0}.sw{width:10px;height:10px;margin-right:5px;vertical-align:-1px}
"""


def verdict_line(rec: Dict[str, Any]) -> str:
    v = rec.get("verdict")
    if v == "change":
        return f"Change your rule (current: {rec.get('current_source')})"
    if v == "keep":
        return f"Keep your current rule ({rec.get('current_source')})"
    return "Warn only: no rule beats doing nothing"


def alt_phrase(alt: Dict[str, Any]) -> str:
    """'Equivalent' only when the out-of-sample gain interval includes 0; otherwise say how much better."""
    gi, gm = alt.get("oob_gain_interval") or [None, None], alt.get("oob_gain_median")
    ivs = f" (out of sample: median {gm:+.1f}, 90% {gi[0]:+.1f} to {gi[1]:+.1f})" if gi[0] is not None else ""
    rel = alt.get("relation")
    if rel == "better, below the bar":
        return f"better by ~{gm:.1f} pts, below the 1-point bar for changing{ivs}"
    if rel == "equivalent":
        return f"equivalent to your current rule{ivs}"
    if rel == "better":
        return f"better by ~{gm:.1f} pts{ivs}"
    return f"{alt['gain_points']:+.1f} pts in sample (not tested)"


def alt_block(rec: Dict[str, Any]) -> str:
    alt = rec.get("alternative")
    if not alt:
        return ""
    if rec["verdict"] == "change":
        return f'<p class=ink2>Was: {esc(rec.get("current_rule"))} — {pc(rec.get("current_saving"), 1)}.</p>'
    return (f'<h3>Best rule found, not adopted</h3><p>{esc(alt["rule"])} — {pc(alt["saving"], 1)} in sample '
            f'({alt["gain_points"]:+.1f} pts); {esc(alt_phrase(alt))}.</p>'
            f'<pre>{esc(json.dumps(alt["settings"], indent=1))}</pre>')


def range_html(rec: Optional[Dict[str, Any]]) -> str:
    ar = (rec or {}).get("assumption_range") or {}
    if ar.get("lo") is None:
        return ""
    unc = f"; <b>{pc(ar['uncached'])}</b> if compaction cannot read the cache" if ar.get("uncached") is not None else ""
    return (f'<p class=warn>Assumption rows span <b>{pc(ar["lo"])}–{pc(ar["hi"])}</b> ({esc(ar["lo_variant"])} … '
            f'{esc(ar["hi_variant"])}){unc}. The 90% interval and the confidence label cover sampling only.</p>')


def rec_card(title: str, rec: Optional[Dict[str, Any]], big: Optional[float] = None, sub: str = "", extra: str = "") -> str:
    if not rec:
        return f'<div class=card><h3>{esc(title)}</h3><p class=muted>Not enough data for this view.</p></div>'
    big = rec["saving"] if big is None else big
    sub = sub or (f'saving vs doing nothing · 90% {esc(interval(rec)) or "n/a"} · n={rec["n"]} · '
                  f'sampling confidence <b>{esc(rec["confidence"])}</b>')
    return (f'<div class=card><h3>{esc(title)}</h3><p><b>{esc(verdict_line(rec))}</b></p><div class=big>{pc(big)}</div>'
            f'<div class=ink2>{sub}</div><p>{esc(rec["rule"])}</p>'
            f'<pre>{esc(json.dumps(rec["settings"], indent=1))}</pre>{alt_block(rec)}'
            f'<p class=muted>{esc(rec["when"])}</p>{extra}</div>')


def build_html(r: Dict[str, Any]) -> str:
    c, g, recs = r["claude_code"], r["global"], r["recommendation"]
    m = r["model"]
    P: List[str] = []
    P.append('<h1>Cache-clock policy study</h1><ul class=lead>' + "".join(f"<li>{esc(x)}</li>" for x in r["summary_lines"]) + "</ul>")

    # recommendations
    gr = recs.get("global")
    g_extra = ""
    if gr:
        g_extra = (f'<p class=ink2>The modelled saving on pooled return times is {pc(gr["model_saving"])} '
                   f'(90% {esc(interval(gr)) or "n/a"}, n={gr["n"]} returns). Not independent evidence: '
                   f'{esc(gr["label"])}.</p>')
    P.append("<h2>Recommendation</h2><div class=cards>" +
             rec_card("From Claude Code behaviour", recs.get("claude_code")) +
             (rec_card(f"Fallback: {gr['label']}", gr, big=gr["saving_on_claude_code_history"],
                       sub=f"this rule on your Claude Code history · sampling confidence <b>{esc(gr['confidence'])}</b>", extra=g_extra)
              if gr else rec_card("Fallback: global", None)) + "</div>" + range_html(recs.get("claude_code")))

    # out of what
    sp = r.get("spend")
    if sp:
        done = [st for st in sp["steps"] if st["collected"]]
        P.append("<h2>Out of what: the saving as a share of your spend</h2><p class=ink2>" + esc(spend_lead(sp)) + "</p>")
        P.append(hbars([(st["step"], st["saving_share"], "best" if i == 0 else "ref") for i, st in enumerate(done)],
                       fmt=share, vmin=0.0, vmax=1.0))
        P.append(table(["denominator", "what it adds", "total (input-token eq.)", "gap cost is this much of it",
                        "saving as a share of it (90%)", "from the row above"], spend_rows_view(sp), num_from=2))
        per = [(t["tool"],) + tuple(kt(t["cost"].get(c)) if c in t["cost"] else "—" for c, _ in CLASS_NAMES)
               + (kt(t["total"]), kt(t["tokens"]), t["responses_without_counts"]) for t in sp["per_tool"]]
        P.append("<h3>Spend per tool in the window</h3>" + table(
            ["tool"] + [n for _, n in CLASS_NAMES] + ["total", "tokens, unweighted", "responses without token counts"], per))
        notes = ["Totals are in input-token equivalents (cache read 0.1×, writes 1.25×/2×, output 5×), the unit the "
                 "saving is in. Unweighted tokens are mostly cache reads, which is why they dwarf the weighted totals."]
        if sp["uncounted_tools"]:
            notes.append("Not in any total (their stores keep no token counts): "
                         + ", ".join(sp["uncounted_tools"]) + ". Their spend is real, so the last share is an upper bound.")
        if not sp["complete"]:
            notes.append("No spend.json: subagents and scripted runs were not collected (run collect.py).")
        P.append("<p class=muted>" + esc(" ".join(notes)) + "</p>")

    # policy chart
    items = [(p["policy"].split(": ", 1)[0] if p["kind"] in ("current", "best") else p["policy"], p["saving"],
              {"reference": "ref", "current": "cur", "best": "best", "bound": "bound"}[p["kind"]]) for p in c["policies"]]
    P.append("<h2>Policy comparison</h2><p class=ink2>Saving vs doing nothing over every Claude Code idle stretch "
             f"(cache cost of the request that follows, plus pings and compactions), acting {c['lead']:g} min before "
             "expiry. Higher is better.</p>")
    P.append(hbars(items, vmin=min(0.0, min((v for _, v, _ in items if v is not None), default=0)), vmax=1.0))
    P.append(table(["policy", "saving", "cost (input-token eq.)"],
                   [(p["policy"], pc(p["saving"], 1), kt(p["total"])) for p in c["policies"]]))
    P.append("<h3>Closest runners-up among simple rules</h3>" +
             table(["rule", "saving"], [(t["rule"], pc(t["saving"], 1)) for t in c["top_rules"]]))

    # coverage
    rows = []
    for t in r["coverage"]["tools"]:
        if t.get("status") in (None, "not-built"):
            rows.append((t["tool"], t.get("status") or "?", "—", "", "", "", "", Raw("")))
            continue
        ver = "verified" if t.get("verified_adapter") else Raw('<span class=warn>unverified adapter</span>')
        span = f"{(t.get('first') or '')[:10]} – {(t.get('last') or '')[:10]}" if t.get("first") else ""
        rows.append((t["tool"], t["status"], ver, t.get("sessions") or 0, t.get("interactive_sessions") or 0,
                     t.get("headless_sessions") or 0, span, Raw(months_spark(t.get("per_month_sessions") or {}))))
    P.append("<h2>Data coverage per tool</h2>" + table(
        ["tool", "status", "adapter", "sessions", "interactive", "headless", "first – last", "sessions / month"], rows, num_from=3))
    pw = r["coverage"].get("power") or {}
    P.append(f'<p class=muted>Sleep log: {esc(pw.get("status", "not read"))}. "empty" = the store exists but holds no '
             'requests. Headless runs (claude -p, codex exec) are counted but excluded from behaviour. Claude Code '
             f'sessions analysed: {c["sessions"]} with responses; TTL by session: '
             + esc(", ".join(f"{kk} {v}" for kk, v in r["ttl"]["mix"].items())) + '.</p>')

    # TTL validation
    v = r["validation"]
    P.append("<h2>TTL validation</h2><p class=ink2>Share of returns that missed the cache (the next request read under half "
             f"the previous context), by time since the previous request. TTL used: <b>{esc(r['ttl']['used'])}</b>"
             f"{' (override)' if r['ttl']['override'] else ' (inferred from cache-write fields)'}. The bucket just past the "
             "TTL is mixed because a request is timed at its first transcript row, up to a minute after the cache was "
             f"touched. A cold return still read a median <b>{kt(m['W'])}</b> from cache (n={m['W_n']}): the shared "
             "system-prompt and tools prefix, kept warm by other sessions — charged at the read price.</p>")
    P.append(hbars([(x["label"], x["rate"], "best") for x in v], vmin=0, vmax=1))
    P.append(table(["gap since previous request", "returns", "misses", "miss rate"],
                   [(x["label"], x["n"], x["misses"], pc(x["rate"], 1)) for x in v]))

    # behaviour
    rt = c["return_table"]
    cnt = c.get("counts") or {}
    P.append("<h2>Behaviour</h2>")
    P.append(f"<p class=ink2>{c['stretches']} idle stretches of at least {round(c['threshold_s'] / 60)} min; context at "
             f"walk-away median {kt(c['ctx_at_walkaway']['median'])}, p75 {kt(c['ctx_at_walkaway']['p75'])}. Never came "
             f"back: {pc(rt['never_share'])}. A turn was still running at the first action in "
             f"{c['midturn_at_first_action']}. Ended by a manual /compact: {c['manual_compact_returns']}. Dropped: "
             f"{cnt.get('censored', 0)} last requests too recent to tell, {cnt.get('ended_by_command', 0)} sessions "
             "ended by a command after their last request (e.g. /exit).</p>")
    labs = rt["return_labels"]
    P.append("<h3>Time away × context at walk-away</h3>" + stacked([(x["label"], x["by_ctx"]) for x in rt["rows"]], rt["ctx_labels"]))
    P.append(table(["time away"] + rt["ctx_labels"] + ["total"] + labs + ["by /compact"],
                   [[x["label"]] + x["by_ctx"] + [x["n"]] + [x.get(l, 0) for l in labs] + [x.get("manual_compact", 0)]
                    for x in rt["rows"]]))
    P.append("<p class=muted>Human = a typed prompt (slash commands included) ended the stretch. Automatic = only a task "
             "notification, wake-up, or cross-session/teammate message. Within a turn = no turn end and no prompt before "
             "the next request: a long tool run or a permission wait — the engine refuses a compaction then, so the mod "
             "pings instead (up to maxKeepAlives pings), which the costs include. Other = a turn end but no prompt.</p>")

    # bands
    bt = c["bands"]
    P.append("<h2>Per-band optimum</h2>" + table(
        ["context band", "n", "cheapest policy", "saving", "runner-up"],
        [(b["label"], b["n"], b["best"], pc(b["best_saving"], 1),
          f'{b["runners_up"][0]["policy"]} {pc(b["runners_up"][0]["saving"], 1)}' if b["runners_up"] else "") for b in bt["bands"]],
        num_from=9))
    P.append(f'<p class=muted>Per-band optimum overall: {pc(bt["per_band_optimum_saving"], 1)}.'
             f'{" Bands under 30 stretches were merged with a neighbour." if bt["merged_small_bands"] else ""}</p>')

    # rigor
    b, h = c["bootstrap"], c["holdout"]
    P.append("<h2>Rigor</h2>")
    if b.get("resamples"):
        P.append(f'<p>Bootstrap resampling <b>sessions</b> ({b["resamples"]} resamples, seed {r["seed"]}). Current rule: '
                 f'{pc(b.get("current_lo"))}–{pc(b.get("current_hi"))} (90%). Best rule: {pc(b.get("lo"))}–{pc(b.get("hi"))}. '
                 f'Paired gain of the best rule over the current one, in sample: median {b["gain_median"]:+.1f} points '
                 f'(90% {b["gain_lo"]:+.1f} to {b["gain_hi"]:+.1f}). That flatters the best rule, which was picked as the '
                 f'maximum of {c.get("n_rules", "the")} rules on the same data. The verdict instead re-selects the best rule in '
                 f'each resample and scores it on the sessions left out: median {fmt_pts(b.get("oob_gain_median"))} points '
                 f'(90% {fmt_pts(b.get("oob_gain_lo"))} to {fmt_pts(b.get("oob_gain_hi"))}; optimism '
                 f'{fmt_pts(b.get("optimism_points"))} points). A change is recommended only if that interval is above 0 '
                 f'and its median is at least 1 point. '
                 f'The current rule is within 1 point of each resample\'s best in {pc(b["current_near_optimal_share"])} '
                 f'of resamples; the best rule in {pc(b["near_optimal_share"])}.</p>')
        P.append(table(["rule (wins a resample)", "share"], [(x["rule"], pc(x["share"], 1)) for x in b["top_winners"]]))
    if h:
        P.append(f'<h3>Temporal holdout</h3><p>Rule chosen on the earliest {h["train_n"]} stretches: {esc(h["train_rule"])}. '
                 f'On the latest {h["test_n"]} (from {esc(h["split_at"])}) it saves {pc(h["test_saving_of_train_rule"], 1)}; '
                 f'your current rule {pc(h["test_current_saving"], 1)}; that period\'s own optimum {pc(h["test_optimum_saving"], 1)} '
                 f'({esc(h["test_optimum_rule"])}) — a gap of {h["gap_points"]:.1f} points for the chosen rule.</p>')
    P.append("<h3>Sensitivity</h3>" + table(
        ["assumption changed", "recommended rule saves", "current rule", "best rule under it", "its saving"],
        [(s["variant"], pc(s["recommended_saving"], 1), pc(s["current_saving"], 1),
          "same as the best rule" if s["same_rule"] else s["variant_best_rule"], pc(s["variant_best_saving"], 1))
         for s in c["sensitivity"]]))
    P.append("<p class=muted>Lead rows: the model has no cost for acting late, so it always prefers the shortest lead; "
             "the mod measures its deadline from the end of the request, so a slow request leaves less margin than the "
             "lead says. The lead is an input (--lead), not a result.</p>")
    if r["warnings"]:
        P.append("<h3>Sample warnings</h3><ul>" + "".join(f"<li class=warn>{esc(w)}</li>" for w in r["warnings"]) + "</ul>")

    # sleep
    s = r["sleep"]
    P.append("<h2>Sleep</h2>")
    if s.get("available"):
        txt = (f'Window {esc(fmt_day(s["window"][0]))} to {esc(fmt_day(s["window"][1]))} ({esc(s["window_source"])}); '
               f'{s["intervals"]} sleep intervals ({s["asleep_hours_in_window"]:.0f} h asleep). Of {s["stretches_in_window"]} '
               f'idle stretches in that window, sleep cancelled a scheduled action in {s["affected"]} ({pc(s["affected_share"])}). '
               f'Saving there: {pc(s["saving_ignoring_sleep"], 1)} ignoring sleep → {pc(s["saving_with_sleep"], 1)} with it.')
        if (s["saving_with_sleep"] or 0) > (s["saving_ignoring_sleep"] or 0):
            txt += " Sleep can raise the saving: an action it cancels on a stretch you never came back to is not paid for."
        wd = s.get("with_darkwake")
        if wd:
            txt += (f' {s["darkwake_intervals"]} lid-closed spans held awake by an assertion ("to DarkWake") are not '
                    f'counted as asleep; counting them: {wd["affected"]} affected, saving {pc(wd["saving_with_sleep"], 1)}.')
        txt += " An action is lost only if the machine sleeps through its whole lead window (the mod acts on waking before expiry)."
        P.append(f"<p>{txt}</p>")
    else:
        P.append("<p class=muted>No sleep log was found, so the saving assumes the machine is awake at every action. "
                 "A sleeping laptop cannot ping or compact; the mod then does nothing.</p>")

    # global
    P.append("<h2>Global presence analysis</h2>")
    pr = r["presence"]
    for key, title in (("other_tools", "Active in another AI tool"), ("any_other_session", "Active in another AI tool or another Claude Code session")):
        x = pr.get(key)
        if not x:
            continue
        cl = x["classes"]
        P.append(f"<h3>{esc(title)} between walk-away and the first action</h3>")
        if "elsewhere" not in cl:
            P.append("<p class=muted>No stretch had such activity" + (" — no other tool's data was found." if key == "other_tools" and g.get("only_claude_code") else ".") + "</p>")
            continue
        P.append(table(["class", "n", "median context", "best rule for the class", "its saving", "recommended rule's saving"],
                       [(lab, d["n"], kt(d["median_ctx"]), d["best_rule"], pc(d["best_saving"], 1), pc(d["blind_rule_saving"], 1))
                        for lab, d in cl.items()], num_from=1))
        P.append(stacked([(lab, [z["n"] for z in d["returns"]]) for lab, d in cl.items()], [z["label"] for z in next(iter(cl.values()))["returns"]]))
        P.append(f'<p>Presence-aware (one rule per class): {pc(x["aware_saving"], 1)} vs one rule {pc(x["blind_saving"], 1)} — '
                 f'<b>{x["value_points"]:.1f} points</b>, in-sample, so an upper bound. Under ~1 point, watching this activity '
                 'is not worth building.</p>')
    P.append(f"<h3>Pooled return times — {esc(g.get('label', 'global'))}</h3>")
    pt = g.get("per_tool", {})
    if pt:
        labels = [z["label"] for z in g["pooled_dist"]]
        P.append(table(["tool", "n"] + labels, [[t, d["n"]] + [z["n"] for z in d["dist"]] for t, d in sorted(pt.items())] +
                       [["pooled", g["pooled_n"]] + [z["n"] for z in g["pooled_dist"]]]))
        P.append(f'<p class=muted>Only returns that end in a typed prompt are kept, but every never-returned session is, so '
                 f'"never" is {pc(g.get("never_share_pooled"))} here against {pc(c["return_table"]["never_share"])} in the '
                 'Claude Code view — a selection effect that favours fewer pings.</p>')
    if g.get("rule"):
        diff = g["points_vs_claude_code_rule"]
        P.append(f'<p>Best rule on pooled return times (paired with Claude Code contexts, assumed independent): '
                 f'<b>{esc(g["rule"])}</b>. On your Claude Code history it saves {pc(g["saving_on_claude_code_history"], 1)} '
                 f'vs {pc(g["claude_code_rule_saving"], 1)} for the Claude Code view\'s best rule '
                 f'({abs(diff):.1f} points {"less" if diff < 0 else "more"}).</p>')
    if g.get("sessions_without_prompts"):
        P.append(f'<p class=muted>Sessions without per-prompt timestamps (they count for presence only): '
                 f'{esc(", ".join(f"{kk} {v}" for kk, v in g["sessions_without_prompts"].items()))}.</p>')
    ce = r["cache_economics"]
    if ce:
        P.append("<h3>Cache economics per tool (as each adapter reports)</h3>" + table(
            ["tool", "responses", "cache-read share of prompt tokens", "cache-write share", "median context"],
            [(x["tool"], x["responses"], pc(x["cache_read_share"], 1), pc(x["cache_write_share"], 1), kt(x["median_ctx"])) for x in ce]))

    # method
    pr_ = r["pricing"]
    P.append("<h2>Method &amp; assumptions</h2>" + table(["assumption", "value"], [
        ("costs", f"input-token equivalents: cache read {pr_['cache_read']}×, 5-min write {pr_['cache_write_5m']}×, "
                  f"1-h write {pr_['cache_write_1h']}×, output {pr_['output']}× (edit references/pricing.json)"),
        ("current rule", f"{r['current']['source']}: {r['current']['rule']}"),
        ("TTL and lead", f"{r['ttl']['used']}; act {r['lead']['used']:g} min before expiry ({r['lead']['source']}); "
                         "first action at TTL − lead, then TTL − lead after each ping (the mod re-arms from the ping)"),
        ("keep-alive ping", f"re-reads the context ({m['read']}×) plus {m['ping_out']:.0f} output tokens; restarts the TTL"),
        ("compaction", f"reads the context from cache ({m['compact_read']}×), writes a {kt(m['summary'])} summary as output; "
                       f"the return writes {kt(m['post'])} (your median first request after a compaction; n={m['n_compactions']})"),
        ("mid-turn", "a compaction is refused while a turn runs: the mod pings instead, up to maxKeepAlives pings in the stretch"),
        ("cold return", f"re-writes the context minus the shared prefix ({kt(m['W'])}, read at {m['read']}×); same for the "
                        "post-compaction write; a return on another model is always cold"),
        ("flags", "; ".join(m["flags"]) or "none"),
        ("idle stretch", "gap between consecutive requests ≥ TTL − 5 min; context = the previous request's prompt size; "
                         "a manual /compact in the gap ends the stretch there"),
        ("never came back", "the CLI is assumed open, so the mod still acts (sensitivity: cost nothing); a session's last "
                            "request counts when the data ends more than 4 TTLs later, else it is dropped"),
        ("independence", "the global rule pairs every Claude Code context with every pooled return time"),
        ("not priced", "follow-up requests after a compaction re-read the small context instead of C (favours compaction, "
                       "so the study is conservative toward pings); compaction loses conversation detail (favours pings)"),
    ], num_from=9))
    return ("<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>Cache policy study</title><style>{CSS}</style></head><body><main>{''.join(P)}"
            f"<p class=muted>Generated {esc(r['generated'])} · analysis took {r['seconds']} s.</p></main></body></html>")


def fmt_pts(x: Optional[float]) -> str:
    return "n/a" if x is None else f"{x:+.1f}"


def fmt_day(t: Optional[float]) -> str:
    import time
    return "?" if t is None else time.strftime("%Y-%m-%d", time.gmtime(t))


def build_md(r: Dict[str, Any]) -> str:
    L = [f"- {x}" for x in r["summary_lines"]]
    L.append("")
    for key, title in (("claude_code", "From Claude Code behaviour"), ("global", "Fallback: global behaviour")):
        rec = r["recommendation"].get(key)
        if not rec:
            continue
        head = rec.get("label", "") if key == "global" else ""
        L += [f"## {title}{' — ' + head if head else ''}", "", f"**{verdict_line(rec)}: {rec['rule']}** — saves "
              f"{pc(rec['saving'])} (90% {interval(rec) or 'n/a'}), n={rec['n']}, sampling confidence {rec['confidence']}.", "",
              "```json", json.dumps(rec["settings"], indent=1), "```", ""]
        alt = rec.get("alternative")
        if alt and rec["verdict"] == "keep":
            L += [f"Best rule found, not adopted: {alt['rule']} — {pc(alt['saving'])}; {alt_phrase(alt)}.", ""]
        ar = rec.get("assumption_range")
        if ar and ar.get("lo") is not None:
            L += [f"Assumption rows span {pc(ar['lo'])}–{pc(ar['hi'])}"
                  + (f"; {pc(ar['uncached'])} if compaction cannot read the cache" if ar.get("uncached") is not None else "")
                  + ". The interval and confidence cover sampling only.", ""]
        if key == "global":
            L += [f"On your Claude Code history: {pc(rec['saving_on_claude_code_history'])}.", ""]
        L += [rec["when"], ""]
    sp = r.get("spend")
    if sp:
        L += ["## Out of what: the saving as a share of your spend", "", spend_lead(sp), "",
              "| denominator | total | gap cost is | saving share (90%) | from row above |", "|---|---:|---:|---:|---:|"]
        L += [f"| {a} | {c} | {d} | {e} | {f} |" for a, _, c, d, e, f in spend_rows_view(sp)]
        L += ["", "| tool | main thread | subagents | scripted runs | total | responses without token counts |",
              "|---|---:|---:|---:|---:|---:|"]
        L += [f"| {t['tool']} | " + " | ".join(kt(t["cost"].get(c)) if c in t["cost"] else "—" for c, _ in CLASS_NAMES)
              + f" | {kt(t['total'])} | {t['responses_without_counts']} |" for t in sp["per_tool"]]
        if sp["uncounted_tools"]:
            L += ["", "No token counts (not in any total): " + ", ".join(sp["uncounted_tools"]) + "."]
        L.append("")
    L += ["## Policies", "", "| policy | saving |", "|---|---:|"]
    L += [f"| {p['policy']} | {pc(p['saving'], 1)} |" for p in r["claude_code"]["policies"]]
    L += ["", "## TTL validation", "", "| gap | returns | misses |", "|---|---:|---:|"]
    L += [f"| {v['label']} | {v['n']} | {v['misses']} |" for v in r["validation"]]
    L += ["", "## Sensitivity", "", "| assumption | recommended rule | best under it |", "|---|---:|---:|"]
    L += [f"| {s['variant']} | {pc(s['recommended_saving'], 1)} | {pc(s['variant_best_saving'], 1)} |"
          for s in r["claude_code"]["sensitivity"]]
    L += ["", "## Coverage", "", "| tool | status | sessions | interactive | adapter |", "|---|---|---:|---:|---|"]
    for t in r["coverage"]["tools"]:
        L.append(f"| {t['tool']} | {t.get('status')} | {t.get('sessions') or 0} | {t.get('interactive_sessions') or 0} | "
                 f"{'verified' if t.get('verified_adapter') else 'unverified'} |")
    if r["warnings"]:
        L += ["", "## Warnings", ""] + [f"- {w}" for w in r["warnings"]]
    L += ["", "Not priced: compaction's loss of detail, and the cheaper follow-up requests after a compaction."]
    return "\n".join(L) + "\n"


def photo_spend(sp: Optional[Dict[str, Any]]) -> str:
    if not sp:
        return ""
    short = {"idle-gap cost": "gap", "Claude Code, interactive, main thread": "cc main", "+ subagents": "+subagents",
             "+ scripted runs (claude -p, SDK)": "+scripted", "+ other AI tools": "+other tools"}
    lines = [f"{short.get(st['step'], st['step'])} {kt(st['denominator'])} → {share(st['saving_share'])}"
             if st["collected"] else f"{short.get(st['step'], st['step'])} not collected" for st in sp["steps"]]
    unc = f"<p><small>no token counts: {esc(' '.join(sp['uncounted_tools']))}</small></p>" if sp["uncounted_tools"] else ""
    return (f"<h2>SAVED {kt(sp['saved'])} AS A SHARE OF</h2><p>" + "<br>".join(esc(x) for x in lines) + "</p>" + unc)


def build_photo(r: Dict[str, Any]) -> str:
    def block(title, rec, extra=""):
        if not rec:
            return f"<section><h2>{esc(title)}</h2><p>no data</p></section>"
        st = rec["settings"]
        alt = rec.get("alternative")
        alt_s = ""
        if alt and rec["verdict"] == "keep":
            alt_s = f"<p><small>alt {esc(alt['rule'])}: {esc(alt_phrase(alt))}</small></p>"
        ar = rec.get("assumption_range") or {}
        if ar.get("lo") is not None:
            alt_s += (f"<p><small>assumptions {pc(ar['lo'])}–{pc(ar['hi'])}"
                      + (f"; {pc(ar['uncached'])} if compaction uncached" if ar.get("uncached") is not None else "") + "</small></p>")
        return (f"<section><h2>{esc(title)}</h2><p>{esc(verdict_line(rec).upper())}</p>"
                f"<p class=big>{pc(rec['saving'])} <small>[{esc(interval(rec)) or 'n/a'}] "
                f"n={rec['n']} sampling {esc(rec['confidence'])}</small></p><p>{esc(rec['rule'])}</p>{extra}"
                f"<p>ttl={esc(st['ttl'])} lead={st['leadMinutes']:g} keepAliveBelow={st['keepAliveBelowTokens']} "
                f"maxKeepAlives={st['maxKeepAlives']} compactAbove={st['compactAboveTokens']} autoAct={str(st['autoAct']).lower()}</p>"
                f"{alt_s}</section>")
    cov = " · ".join(f"{t['tool']} {t.get('status')}" + (f" {t.get('interactive_sessions') or 0}/{t.get('sessions') or 0}"
                                                          + ("" if t.get("verified_adapter") else " UNVERIFIED")
                                                          if t.get("status") not in (None, "not-built") else "")
                     for t in r["coverage"]["tools"])
    c = r["claude_code"]
    gr = r["recommendation"].get("global")
    return ("<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            "<title>Cache policy photo</title><style>"
            ":root{color-scheme:light;--bg:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e}"
            "@media (prefers-color-scheme:dark){:root:not([data-theme=\"light\"]){color-scheme:dark;--bg:#1a1a19;--ink:#fff;--ink2:#c3c2b7}}"
            ":root[data-theme=\"dark\"]{color-scheme:dark;--bg:#1a1a19;--ink:#fff;--ink2:#c3c2b7}"
            "body{margin:0;background:var(--bg);color:var(--ink);font:22px/1.35 ui-monospace,SFMono-Regular,Menlo,monospace}"
            "main{padding:16px 24px}h1{font-size:26px;margin:0 0 8px}h2{font-size:22px;margin:14px 0 2px;color:var(--ink2)}"
            "p{margin:2px 0}.big{font-size:40px;font-weight:700}small{font-size:20px;color:var(--ink2);font-weight:400}"
            "</style></head><body><main>"
            f"<h1>cache-clock study · TTL {esc(r['ttl']['used'])} · current ({esc(r['current']['source'])}) {pc(c['current_saving'])}</h1>"
            + block("CLAUDE CODE VIEW", r["recommendation"].get("claude_code"))
            + (block("FALLBACK " + gr["label"].upper(), gr,
                     f"<p>on Claude Code history: {pc(gr.get('saving_on_claude_code_history'))} (not independent)</p>")
               if gr else "")
            + photo_spend(r.get("spend"))
            + f"<h2>COVERAGE</h2><p>{esc(cov)}</p>"
            + f"<p>stretches={c['stretches']} midturn={c['midturn_at_first_action']} never={pc(c['return_table']['never_share'])} "
              f"ctx_med={kt(c['ctx_at_walkaway']['median'])} W={kt(r['model']['W'])}</p>"
            "</main></body></html>")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run", required=True)
    a = ap.parse_args(argv)
    run = Path(a.run)
    r = json.loads((run / "results.json").read_text())
    (run / "report.html").write_text(build_html(r))
    (run / "summary.md").write_text(build_md(r))
    (run / "photo.html").write_text(build_photo(r))
    print(f"wrote report.html, summary.md, photo.html in {run}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
