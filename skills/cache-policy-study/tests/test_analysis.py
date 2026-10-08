"""Analysis tests on SYNTHETIC events. Run from the skill root:

    python3 -m unittest discover -s tests -p 'test_analysis*.py'
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import analyze  # noqa: E402
import report  # noqa: E402

H = 3600.0
MIN = 60.0
M = {"read": 0.1, "write": 2.0, "out": 5.0, "compact_read": 0.1, "summary": 1000.0, "post": 10000.0, "ping_out": 60.0,
     "W": 0.0}
PING = 100e3 * 0.1 + 60 * 5          # 10,300: one keep-alive on a 100k context
NO_SETTINGS = Path("/nonexistent/settings.json")   # tests never read the real user's cache-clock settings


# ----------------------------------------------------------------------------- synthetic event generator

class Gen:
    """Builds events.jsonl rows the way the adapters do."""

    def __init__(self):
        self.rows = []

    def ev(self, tool, sid, t, kind, fidelity="turn", usage=None, compaction=None, interactive=True, model="m-1"):
        self.rows.append({"tool": tool, "session": sid, "t": float(t), "kind": kind, "interactive": interactive,
                          "fidelity": fidelity, "project": "p000000000", "model": model, "subagent": False,
                          "usage": usage, "compaction": compaction})

    def resp(self, sid, t, ctx, read=None, ttl="1h", tool="claude-code", model="m-1"):
        w = 500
        self.ev(tool, sid, t, "response", model=model, usage={
            "input": 5, "output": 100, "cache_read": ctx - w if read is None else read, "cache_write": w,
            "cache_write_5m": w if ttl == "5m" else 0, "cache_write_1h": w if ttl == "1h" else 0, "ctx": ctx})

    def walk(self, sid, t0, ctx, gap, human=True, ret_ctx=None, turn_end=True):
        """A Claude Code session: one warm request, walk away at t0 with ctx, come back after gap (None = never)."""
        self.ev("claude-code", sid, t0 - 120, "prompt")
        self.resp(sid, t0 - 60, ctx - 1000)
        self.resp(sid, t0, ctx)
        if turn_end:
            self.ev("claude-code", sid, t0 + 1, "turn_end")
        if gap is not None:
            self.ev("claude-code", sid, t0 + gap - 5, "prompt" if human else "auto_prompt")
            miss = gap > H
            self.resp(sid, t0 + gap, ret_ctx or 1000, read=0 if miss else (ret_ctx or 1000) - 500)

    def write(self, d: Path, sleep=None, tools=("claude-code",)):
        self.rows.sort(key=lambda r: r["t"])
        with open(d / "events.jsonl", "w") as f:
            for r in self.rows:
                f.write(json.dumps(r) + "\n")
        cov = {"generated": "2026-10-07T00:00:00Z", "system": "Darwin", "tools": [
            {"tool": t, "status": "ok", "verified_adapter": t == "claude-code", "sessions": 3, "interactive_sessions": 3,
             "headless_sessions": 0, "first": "2026-01-01T00:00:00+00:00", "last": "2026-02-01T00:00:00+00:00",
             "per_month_sessions": {"2026-01": 3}, "events": 10} for t in tools]}
        (d / "coverage.json").write_text(json.dumps(cov))
        if sleep:
            (d / "sleep.json").write_text(json.dumps({
                "intervals": [{"start": a, "end": b, "reason": why} for a, b, why in sleep],
                "window": [min(a for a, _, _ in sleep) - 3600, max(r["t"] for r in self.rows)]}))


def run_analysis(g: Gen, **kw):
    tmp = tempfile.TemporaryDirectory()
    d = Path(tmp.name)
    g.write(d, **{k: v for k, v in kw.items() if k in ("sleep", "tools")})
    r = analyze.analyze(d, n_res=kw.get("n_res", 60), ttl_opt=kw.get("ttl", "auto"),
                        settings_path=kw.get("settings", NO_SETTINGS))
    return tmp, d, r


# ----------------------------------------------------------------------------- cost model

class CostModel(unittest.TestCase):
    """One stretch per policy, hand-computed (C = 100k, TTL 1h, lead 3: actions at 57, 114, 171 min)."""

    def c(self, g, pings, compact, cap=3, m=M, **kw):
        return analyze.cost(g, 100e3, pings, compact, cap, H, 3.0, m, **kw)

    def test_do_nothing(self):
        self.assertEqual(self.c(None, 0, 0), 0.0)                 # never came back: nothing to re-write
        self.assertEqual(self.c(2 * H, 0, 0), 200e3)               # cold: write 100k at 2×
        self.assertAlmostEqual(self.c(3500, 0, 0), 10e3)           # still warm: read 100k at 0.1×

    def test_ping(self):
        self.assertAlmostEqual(self.c(110 * MIN, 1, 0), PING + 10e3)    # ping at 57 m keeps it warm till 117 m
        self.assertAlmostEqual(self.c(118 * MIN, 1, 0), PING + 200e3)
        self.assertAlmostEqual(self.c(None, 2, 0), 2 * PING)        # both pings wasted
        self.assertAlmostEqual(self.c(3000, 3, 0), 10e3)            # back before the first action

    def test_cadence_matches_the_mod(self):
        """F5: the mod re-arms from each ping: actions at 57, 114, 171 min, not 57, 117, 177."""
        self.assertAlmostEqual(self.c(116 * MIN, 2, 0), 2 * PING + 10e3)    # second ping fired at 114 m
        self.assertAlmostEqual(self.c(171.5 * MIN, 3, 0), 3 * PING + 10e3)  # third ping fired at 171 m
        self.assertAlmostEqual(self.c(113 * MIN, 2, 0), PING + 10e3)        # back before the second action

    def test_compact(self):
        self.assertAlmostEqual(self.c(2 * H, 0, 1), 10e3 + 5e3 + 20e3)      # read + summary + post-compaction write
        self.assertAlmostEqual(self.c(None, 0, 1), 10e3 + 5e3)
        self.assertAlmostEqual(self.c(2 * H, 1, 1), PING + 35e3)            # ping at 57 m, compact at 114 m
        self.assertAlmostEqual(self.c(110 * MIN, 1, 1), PING + 10e3)        # back before the compaction

    def test_midturn_compaction_becomes_ping(self):
        """F1: a compaction is refused while the turn runs; the mod pings instead, up to cap pings in all."""
        self.assertAlmostEqual(self.c(None, 0, 1, cap=2, te=None), 2 * PING)          # 57 ping, 114 ping, 171 stop
        self.assertAlmostEqual(self.c(4 * H, 0, 1, cap=2, te=None), 2 * PING + 200e3)  # lapses after the cap
        self.assertAlmostEqual(self.c(4 * H, 1, 1, cap=1, te=None), PING + 200e3)      # the regular ping used the cap
        # the turn ends at 60 min: ping at 57 (mid-turn), compaction at 114 goes through
        self.assertAlmostEqual(self.c(4 * H, 0, 1, cap=3, te=60 * MIN), PING + 35e3)
        # pings are unaffected by a running turn
        self.assertAlmostEqual(self.c(110 * MIN, 1, 0, te=None), PING + 10e3)

    def test_shared_prefix_on_cold_returns(self):
        """F6: a cold return re-writes C − W and reads W; the post-compaction write likewise."""
        m = dict(M, W=20e3)
        self.assertAlmostEqual(self.c(2 * H, 0, 0, m=m), 2 * 80e3 + 0.1 * 20e3)
        self.assertAlmostEqual(self.c(2 * H, 0, 1, m=m), 10e3 + 5e3 + 0.1 * 10e3)   # post 10k < W: all read
        self.assertAlmostEqual(self.c(3500, 0, 0, m=m), 10e3)                        # warm return unchanged

    def test_manual_compact_return_is_the_users_compaction(self):
        """F3: the person's own /compact is paid whatever the policy; a mod compaction makes it moot."""
        self.assertAlmostEqual(self.c(30 * MIN, 0, 0, manual=True), 10e3 + 5e3 + 20e3)    # warm: 0.1×C read
        self.assertAlmostEqual(self.c(2 * H, 0, 0, manual=True), 100e3 + 5e3 + 20e3)      # cold: 1.0×C uncached
        self.assertAlmostEqual(self.c(2 * H, 0, 1, manual=True), 10e3 + 5e3 + 20e3)       # the mod compacted at 57 m

    def test_model_switch_return_is_cold(self):
        self.assertAlmostEqual(self.c(30 * MIN, 0, 0, switch=True), 200e3)

    def test_sleep_blocks_action(self):
        asleep = lambda t: 4000.0 if 3000 <= t <= 4000 else None    # asleep through the 57-min deadline and lead
        self.assertAlmostEqual(self.c(2 * H, 1, 0, asleep=asleep, t0=0.0), 200e3)
        self.assertAlmostEqual(self.c(2 * H, 0, 1, asleep=asleep, t0=0.0), 200e3)
        woke = lambda t: 3500.0 if 3000 <= t <= 3500 else None      # wakes inside the lead window: the mod acts
        self.assertAlmostEqual(self.c(110 * MIN, 1, 0, asleep=woke, t0=0.0), PING + 10e3)

    def test_rule_matches_decide(self):
        rule = (125e3, 300e3, 3)
        self.assertEqual(analyze.rule_atom(rule, 50e3), (3, 0, 0))
        self.assertEqual(analyze.rule_atom(rule, 200e3), (1, 1, 3))
        self.assertEqual(analyze.rule_atom(rule, 300e3), (0, 1, 3))


# ----------------------------------------------------------------------------- search and verdict

def planted() -> Gen:
    """Small contexts come back at 150 m or never (2 pings optimal); mid ones at 100 m or 5 h (ping then compact);
    large ones at 5 h or never (compact at once)."""
    g = Gen()
    t = 1.8e9
    for i in range(40):
        for ctx, gap in ((60e3, 150 * 60), (60e3, None), (200e3, 100 * 60), (200e3, 5 * H), (500e3, 5 * H), (500e3, None)):
            g.walk(f"s{i}-{int(ctx)}-{gap}", t, int(ctx), gap)
            t += 12 * H
    g.ev("claude-code", "end", t + 10 * H, "activity")
    return g


class PlantedBehaviour(unittest.TestCase):
    def test_search_recovers_rule_but_keeps_an_equivalent_current_rule(self):
        """F2: the best rule (2 pings) beats the defaults (3 pings) only by the never-returners' third ping —
        well under a point — so the verdict is keep, with the best rule as the alternative."""
        tmp, _, r = run_analysis(planted())
        with tmp:
            rec = r["recommendation"]["claude_code"]
            self.assertEqual(rec["verdict"], "keep")
            self.assertEqual(rec["current_source"], "mod defaults")
            self.assertEqual(rec["settings"]["maxKeepAlives"], 3)
            alt = rec["alternative"]["settings"]
            self.assertEqual(alt["maxKeepAlives"], 2)
            self.assertTrue(60e3 < alt["keepAliveBelowTokens"] <= 200e3, alt)
            self.assertTrue(200e3 < alt["compactAboveTokens"] <= 500e3, alt)
            self.assertEqual(alt["leadMinutes"], 3.0)          # the rule search holds the lead; lead_fit varies it
            self.assertLess(rec["alternative"]["gain_median"], 1.0)

    def test_change_when_current_settings_are_clearly_worse(self):
        """The user's own settings (1 keep-alive) lose every 150-minute small-context return: change."""
        with tempfile.TemporaryDirectory() as sd:
            sp = Path(sd) / "settings.json"
            sp.write_text(json.dumps({"pluginConfigs": {"cache-clock@local": {"options": {
                "maxKeepAlives": 1, "keepAliveBelowTokens": 125000, "compactAboveTokens": 300000, "leadMinutes": 3}}}}))
            tmp, _, r = run_analysis(planted(), settings=sp)
            with tmp:
                rec = r["recommendation"]["claude_code"]
                self.assertEqual(rec["current_source"], "your settings")
                self.assertEqual(rec["verdict"], "change")
                self.assertGreaterEqual(rec["settings"]["maxKeepAlives"], 2)
                self.assertGreater(r["claude_code"]["bootstrap"]["gain_lo"], 0)

    def test_relation_wording(self):
        """N1: 'equivalent' only when the out-of-sample gain interval includes 0."""
        self.assertEqual(analyze.relation(-0.3, 0.2), "equivalent")
        self.assertEqual(analyze.relation(0.5, 0.9), "better, below the bar")
        self.assertEqual(analyze.relation(0.2, 1.4), "better")
        alt = {"rule": "r", "gain_points": 0.9, "oob_gain_interval": [0.5, 1.5], "oob_gain_median": 0.9,
               "relation": "better, below the bar"}
        phrase = report.alt_phrase(alt)
        self.assertIn("better by ~0.9 pts, below the 1-point bar for changing", phrase)
        self.assertNotIn("equivalent", phrase)
        self.assertNotIn("equivalent", report.alt_phrase(dict(alt, relation="better", oob_gain_median=1.2)))
        self.assertIn("equivalent", report.alt_phrase(dict(alt, oob_gain_interval=[-0.4, 0.6], relation="equivalent")))

    def test_verdict_reselects_out_of_sample(self):
        """N2: the in-sample best rule looks better than it is (it is the maximum over ~200 rules); the verdict
        uses the gain of a rule re-selected in each resample and scored on the sessions left out."""
        import random
        rnd = random.Random(3)
        g = Gen()
        t = 1.8e9
        for i in range(150):
            ctx = rnd.choice([60e3, 150e3, 250e3, 450e3])
            gap = rnd.choice([None, 70 * MIN, 130 * MIN, 4 * H, 10 * H])
            g.walk(f"n{i}", t, int(ctx), gap)
            t += 12 * H
        g.ev("claude-code", "end", t + 10 * H, "activity")
        tmp, _, r = run_analysis(g, n_res=120)
        with tmp:
            b = r["claude_code"]["bootstrap"]
            self.assertEqual(b["oob_resamples"], 120)
            self.assertGreater(b["optimism_points"], 0)
            self.assertLess(b["oob_gain_median"], b["gain_median"] + 1e-9)
            rel = analyze.relation(b["oob_gain_lo"], b["oob_gain_median"])
            self.assertEqual(r["recommendation"]["claude_code"]["verdict"], "change" if rel == "better" else "keep")

    def test_read_current_falls_back_to_defaults(self):
        cur, src = analyze.read_current(NO_SETTINGS)
        self.assertEqual(src, "mod defaults")
        self.assertEqual((cur["A"], cur["B"], cur["N"], cur["lead"]), (125e3, 300e3, 3, 3.0))


# ----------------------------------------------------------------------------- stretch construction

class Stretches(unittest.TestCase):
    def test_censoring_and_exit(self):
        g = Gen()
        g.walk("old", 1.8e9, 100_000, None)                  # data ends 10 h later: never came back
        g.walk("recent", 1.8e9 + 8 * H, 100_000, None)       # data ends 2 h later: cannot tell, dropped
        g.walk("exit", 1.8e9 + H, 100_000, None)
        g.ev("claude-code", "exit", 1.8e9 + H + 60, "prompt")   # /exit after the last response: the mod stops
        g.ev("claude-code", "x", 1.8e9 + 10 * H, "activity")
        tmp, _, r = run_analysis(g)
        with tmp:
            self.assertEqual(r["claude_code"]["counts"]["censored"], 1)
            self.assertEqual(r["claude_code"]["counts"]["ended_by_command"], 1)
            self.assertEqual(r["claude_code"]["returns"].get("never"), 1)

    def test_manual_compact_ends_the_stretch(self):
        """F3: /compact typed 2 h after walk-away ends the stretch there, as a human return."""
        g = Gen()
        t0 = 1.8e9
        g.walk("c", t0, 400_000, None)
        g.ev("claude-code", "c", t0 + 2 * H, "prompt")                       # the typed /compact
        g.ev("claude-code", "c", t0 + 2 * H + 90, "compaction", compaction={"pre": 400_000, "post": 15_000, "trigger": "manual"})
        g.resp("c", t0 + 2 * H + 200, 30_000, read=0)
        g.ev("claude-code", "c", t0 + 2 * H + 201, "turn_end")
        g.ev("claude-code", "z", t0 + 30 * H, "activity")
        tmp, _, r = run_analysis(g)
        with tmp:
            st = analyze.build_cc(analyze.load_events(Path(tmp.name)), "auto", t0 + 30 * H, 3.0, analyze.MOD_DEFAULTS)
            ends = [s for s in st["stretches"] if s["end"] == "compaction"]
            self.assertEqual(len(ends), 1)
            self.assertAlmostEqual(ends[0]["g"], 2 * H + 90)
            self.assertEqual(ends[0]["ret"], "human")
            self.assertEqual(r["claude_code"]["manual_compact_returns"], 1)
            self.assertEqual(r["model"]["post"], 30_000)          # first request after the compaction
            # the post-compaction request is not a new walk-away from the old context
            self.assertFalse(any(s["C"] == 400_000 and s["end"] == "never" for s in st["stretches"]))

    def test_within_a_turn(self):
        """F1/F7: no prompt and no turn end before the next request = the turn was running (tool or permission)."""
        g = Gen()
        t0 = 1.8e9
        g.ev("claude-code", "w", t0 - 120, "prompt")
        g.resp("w", t0, 300_000)
        g.resp("w", t0 + 3 * H, 301_000, read=0)       # tool result after 3 h
        g.ev("claude-code", "w", t0 + 3 * H + 5, "turn_end")
        g.walk("h", t0 + 10 * H, 300_000, 3 * H)        # an ordinary human return
        g.ev("claude-code", "z", t0 + 40 * H, "activity")
        tmp, _, r = run_analysis(g)
        with tmp:
            self.assertEqual(r["claude_code"]["returns"].get("within a turn"), 1)
            self.assertEqual(r["claude_code"]["returns"].get("human"), 1)
            self.assertEqual(r["claude_code"]["midturn_at_first_action"], 1)

    def test_five_minute_sessions(self):
        g = Gen()
        for i in range(3):
            g.ev("claude-code", f"f{i}", 1.8e9 + i * H, "prompt")
            g.resp(f"f{i}", 1.8e9 + i * H + 1, 50_000, ttl="5m")
            g.ev("claude-code", f"f{i}", 1.8e9 + i * H + 600, "prompt")
            g.resp(f"f{i}", 1.8e9 + i * H + 601, 51_000, read=0, ttl="5m")
        g.ev("claude-code", "z", 1.8e9 + 5 * H, "activity")
        tmp, _, r = run_analysis(g)
        with tmp:
            self.assertEqual(r["ttl"]["used"], "5m")
            self.assertEqual(r["claude_code"]["stretches"], 3 + 3)   # three 10-minute gaps + three never-returned

    def test_miss_line_states_the_edge_bucket(self):
        val = [{"lo_s": 300, "hi_s": 3300, "n": 100, "misses": 1}, {"lo_s": 3300, "hi_s": 3600, "n": 10, "misses": 0},
               {"lo_s": 3600, "hi_s": 3900, "n": 10, "misses": 7}, {"lo_s": 3900, "hi_s": 7200, "n": 50, "misses": 50},
               {"lo_s": 7200, "hi_s": None, "n": 50, "misses": 49}]
        self.assertEqual(analyze.miss_line(val), "<55m 1% · 60m–65m 70% · >65m 99%")


# ----------------------------------------------------------------------------- global view and sleep

class Presence(unittest.TestCase):
    def test_classifier(self):
        g = Gen()
        t = 1.8e9
        g.walk("a", t, 200_000, 3 * H)
        g.ev("codex", "cx1", t + 600, "prompt")                           # during the stretch: elsewhere (turn)
        g.walk("b", t + 10 * H, 200_000, 3 * H)
        g.ev("codex", "cx2", t + 10 * H + 2 * H, "prompt")                # after the first action: away
        g.walk("c", t + 20 * H, 200_000, 3 * H)
        g.ev("gemini-cli", "gm", t + 20 * H - 1000, "session_start", fidelity="session")
        g.ev("gemini-cli", "gm", t + 20 * H + 1000, "session_end", fidelity="session")   # overlaps: elsewhere (session)
        g.ev("claude-code", "z", t + 40 * H, "activity")
        tmp, _, r = run_analysis(g, tools=("claude-code", "codex", "gemini-cli"))
        with tmp:
            pr = r["presence"]["other_tools"]
            self.assertEqual(pr["classes"]["elsewhere"]["n"], 2)
            self.assertEqual(r["presence"]["elsewhere_evidence_fidelity"], {"turn": 1, "session": 1})
            self.assertFalse(r["global"]["only_claude_code"])
            self.assertIn("Codex", r["global"]["label"])

    def test_degrades_to_claude_code_only(self):
        g = Gen()
        for i in range(5):
            g.walk(f"s{i}", 1.8e9 + i * 10 * H, 150_000, 2 * H)
        g.ev("claude-code", "z", 1.8e9 + 80 * H, "activity")
        tmp, _, r = run_analysis(g)
        with tmp:
            self.assertTrue(r["global"]["only_claude_code"])
            self.assertEqual(r["global"]["label"], "global (100% Claude Code)")
            self.assertNotIn("elsewhere", r["presence"]["other_tools"]["classes"])
            self.assertIn("global", r["recommendation"])


class SleepMask(unittest.TestCase):
    def test_sleep_window_darkwake_and_share(self):
        g = Gen()
        t = 1.8e9
        for i in range(4):
            g.walk(f"s{i}", t + i * 10 * H, 200_000, 3 * H)
        g.ev("claude-code", "z", t + 60 * H, "activity")
        sleep = [(t + 30 * 60, t + 2.9 * H, "Clamshell Sleep"),                 # through the first stretch's actions
                 (t + 10 * H + 30 * 60, t + 12.9 * H, "Clamshell Sleep (to DarkWake)")]   # uncertain: sensitivity only
        tmp, _, r = run_analysis(g, sleep=sleep)
        with tmp:
            s = r["sleep"]
            self.assertTrue(s["available"])
            self.assertEqual(s["window_source"], "sleep.json window")
            self.assertGreaterEqual(s["stretches_in_window"], 4)
            self.assertEqual(s["affected"], 1)
            self.assertEqual(s["darkwake_intervals"], 1)
            self.assertEqual(s["with_darkwake"]["affected"], 2)


class LeadFit(unittest.TestCase):
    def test_five_minute_ttl_acts_late(self):
        """5-minute TTL, people back after 4:05-4:25: acting at 2:30 or 4:00 pings and compacts for nothing; 4:30 does not."""
        g = Gen()
        t = 1.8e9
        for i in range(80):
            g.walk(f"s{i}", t + i * 3 * H, 400_000 if i % 2 else 80_000, 245 + (i % 5) * 5)
        with tempfile.TemporaryDirectory() as sd:
            sp = Path(sd) / "settings.json"
            sp.write_text(json.dumps({"pluginConfigs": {"cache-clock@local": {"options": {"leadMinutes5m": 2.5}}}}))
            tmp, _, r = run_analysis(g, ttl="5m", settings=sp)
            with tmp:
                lf = r["lead"]["fit"]
                self.assertEqual((lf["key"], lf["current_lead"], lf["best_lead"], lf["verdict"]), ("leadMinutes5m", 2.5, 0.5, "change"))
                self.assertGreater(lf["gain_interval"][0], 0)
                self.assertIn("Act later: leadMinutes5m 0.5 instead of 2.5", " ".join(r["summary_lines"]))
                self.assertGreaterEqual(min(x["lead"] for x in lf["rows"]), 0.5)        # never inside 30 s


class SpendLadder(unittest.TestCase):
    def test_shares_and_factors(self):
        z = dict.fromkeys(("cache_read", "cache_write_5m", "cache_write_1h", "cache_write_other"), 0)
        def row(tool, cls, inp, day="2026-01-10", with_usage=1):
            return {"tool": tool, "class": cls, "day": day, "responses": 1, "with_usage": with_usage,
                    "input": inp, "output": 0, **z}
        rows = [row("claude-code", "main", 1000), row("claude-code", "subagent", 1000),
                row("claude-code", "headless", 2000), row("codex", "main", 1000),
                row("claude-code", "main", 9999, day="2025-12-31"),                # outside the window
                row("cursor", "main", 0, with_usage=0)]                           # no token counts
        pricing = {"input": 1.0, "output": 5.0, "cache_read": 0.1, "cache_write_5m": 1.25, "cache_write_1h": 2.0}
        sp = analyze.spend_ladder(rows, True, ("2026-01-01", "2026-01-31"), pricing, {"codex": {"input": 0.5}}, "1h",
                                  base=200.0, rec={"saving": 0.5, "interval": [0.4, 0.6]})
        st = sp["steps"]
        self.assertEqual([s["denominator"] for s in st], [200.0, 1000.0, 2000.0, 4000.0, 4500.0])
        self.assertEqual(sp["saved"], 100.0)
        self.assertAlmostEqual(st[1]["saving_share"], 0.1)
        self.assertAlmostEqual(st[4]["saving_share"], 100 / 4500)
        self.assertAlmostEqual(st[2]["factor"], 0.5)
        self.assertAlmostEqual(st[1]["interval"][0], 0.08)          # 40% of the gap × 200 / 1000
        for prev, cur in zip(st, st[1:]):                           # share = previous share × factor
            self.assertAlmostEqual(cur["saving_share"], prev["saving_share"] * cur["factor"])
        self.assertEqual(sp["uncounted_tools"], ["cursor"])

    def test_without_spend_json_only_the_main_thread(self):
        g = Gen()
        t = 1.8e9
        for i in range(12):
            g.walk(f"s{i}", t + i * 8 * H, 400_000, 2 * H)
        tmp, d, r = run_analysis(g)
        with tmp:
            st = r["spend"]["steps"]
            self.assertTrue(st[1]["collected"])
            self.assertFalse(any(s["collected"] for s in st[2:]))
            self.assertIn("no spend.json", " ".join(r["warnings"]))
            self.assertIn("of your interactive Claude Code main-thread spend", r["summary_lines"][0])


class ReportSmoke(unittest.TestCase):
    def test_files_and_no_identifiers(self):
        g = Gen()
        t = 1.8e9
        for i in range(30):
            g.walk(f"secret-session-{i:04d}", t + i * 8 * H, 80_000 + 20_000 * (i % 20), (1 + i % 5) * H if i % 4 else None)
            g.ev("codex", f"codex-sess-{i:04d}", t + i * 8 * H + 900, "prompt")
            g.ev("codex", f"codex-sess-{i:04d}", t + i * 8 * H + 4 * H, "prompt")
        g.ev("claude-code", "z", t + 300 * H, "activity")
        tmp, d, r = run_analysis(g, tools=("claude-code", "codex"))
        with tmp:
            (d / "results.json").write_text(json.dumps(r))
            self.assertEqual(report.main(["--run", str(d)]), 0)
            for name in ("report.html", "summary.md", "photo.html"):
                self.assertTrue((d / name).exists(), name)
            self.assertLessEqual(len(r["summary_lines"]), 5)
            self.assertNotIn(" ai ", " ".join(r["summary_lines"]))
            photo = (d / "photo.html").read_text()
            self.assertIsNone(re.search(r"(/Users/|/home/|[A-Za-z]:\\\\|/private/|/tmp/)", photo))
            for name in ("report.html", "summary.md", "photo.html"):
                text = (d / name).read_text()
                self.assertNotIn("secret-session", text)
                self.assertNotIn("codex-sess", text)
                self.assertNotIn("p000000000", text)


if __name__ == "__main__":
    unittest.main()
