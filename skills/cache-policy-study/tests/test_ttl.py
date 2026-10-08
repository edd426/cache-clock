"""5-minute vs 1-hour TTL comparison on SYNTHETIC request sequences. Run from the skill root:

    python3 -m unittest discover -s tests -p 'test_ttl*.py'
"""
from __future__ import annotations

import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import ttl  # noqa: E402

P = {"input": 1.0, "output": 5.0, "cache_read": 0.1, "cache_write_5m": 1.25, "cache_write_1h": 2.0}
MIN = 60.0


def req(lane, t, read, write, at="1h", cls="headless", model="m"):
    u = {"input": 0, "output": 0, "cache_read": read, "cache_write": write,
         "cache_write_5m": write if at == "5m" else 0, "cache_write_1h": write if at == "1h" else 0, "ctx": read + write}
    return {"lane": lane, "class": cls, "t": float(t), "kind": "response", "model": model, "usage": u}


def costs(rows, W=0.0, f=1.0):
    lanes = ttl.lanes_of(rows)
    out = {}
    for c, ls in lanes.items():
        if ls:
            out[c] = ttl.lane_costs(ls, P, W, f)["totals"]
    return out


class PairModel(unittest.TestCase):
    def test_rapid_requests_only_pay_the_premium(self):
        # every request seconds apart: both TTLs read the same, 1h pays 0.75x more on every written token
        rows = [req("a", 0, 0, 100_000)] + [req("a", 10 * i, 100_000 + 1000 * (i - 1), 1000) for i in range(1, 10)]
        T = costs(rows)["headless"]
        writes = 100_000 + 9 * 1000
        self.assertAlmostEqual(T["1h"] - T["5m"], 0.75 * writes)
        self.assertAlmostEqual(T["1h"], T["observed"])          # the history ran at 1h: the model reproduces it

    def test_thirty_minute_return(self):
        # 1h history: the return read 100k. Under 5m, no other lane within 5 minutes: it writes all 101k.
        rows = [req("a", 0, 0, 100_000), req("a", 30 * MIN, 100_000, 1000)]
        T = costs(rows)["headless"]
        self.assertAlmostEqual(T["1h"], 100_000 * 2.0 + 100_000 * 0.1 + 1000 * 2.0)
        self.assertAlmostEqual(T["5m"], 100_000 * 1.25 + 101_000 * 1.25)
        self.assertGreater(T["5m"], T["1h"])

    def test_five_minute_history_models_the_hour(self):
        # 5m history: the 30-minute return was cold (read 0). Under 1h it reads f x min(previous, current).
        rows = [req("a", 0, 0, 100_000, at="5m"), req("a", 30 * MIN, 0, 101_000, at="5m")]
        T = costs(rows, f=0.8)["headless"]
        self.assertAlmostEqual(T["5m"], 201_000 * 1.25)
        r1 = 0.8 * 100_000
        self.assertAlmostEqual(T["1h"], 100_000 * 2.0 + r1 * 0.1 + (101_000 - r1) * 2.0)

    def test_new_lane_reuses_a_neighbours_prefix_only_within_the_ttl(self):
        # lane b starts 2 minutes after a: its 20k shared-prefix read survives under 5m; lane c starts 30 min later:
        # under 5m it must write the 20k, under 1h it still reads it
        rows = [req("a", 0, 0, 50_000), req("b", 2 * MIN, 20_000, 30_000), req("c", 32 * MIN, 20_000, 30_000)]
        lc = ttl.lane_costs(ttl.lanes_of(rows)["headless"], P, 0.0, 1.0)["per_session"]
        self.assertAlmostEqual(lc["b"]["5m"], 20_000 * 0.1 + 30_000 * 1.25)
        self.assertAlmostEqual(lc["c"]["5m"], 50_000 * 1.25)
        self.assertAlmostEqual(lc["c"]["1h"], 20_000 * 0.1 + 30_000 * 2.0)

    def test_compaction_and_model_switch_restart_the_lane(self):
        rows = [req("a", 0, 0, 100_000), {"lane": "a", "class": "main", "t": 60.0, "kind": "compaction",
                                         "model": None, "usage": None}, req("a", 30 * MIN, 5_000, 20_000, cls="main")]
        rows[0]["class"] = "main"
        gaps = ttl.lane_costs(ttl.lanes_of(rows)["main"], P, 0.0, 1.0)["gaps"]
        self.assertEqual(gaps["first"], 2)

    def test_survival_is_measured_on_hour_returns(self):
        rows = [req("a", 0, 0, 100_000), req("a", 20 * MIN, 80_000, 21_000)]
        f, n = ttl.survival(ttl.lanes_of(rows)["headless"])
        self.assertEqual(n, 1)
        self.assertAlmostEqual(f, 0.8)


    def test_model_check_can_fail(self):
        # the hour actually served only half the context on the return; the blind model (f=1) predicts all of it
        rows = [req("a", 0, 0, 100_000), req("a", 30 * MIN, 50_000, 51_000)]
        lanes = ttl.lanes_of(rows)["headless"]
        trusted = ttl.lane_costs(lanes, P, 0.0, 1.0)["totals"]
        blind = ttl.lane_costs(lanes, P, 0.0, 1.0, trust_obs=False)["totals"]
        self.assertAlmostEqual(trusted["1h"], trusted["observed"])
        self.assertLess(blind["1h"], blind["observed"])


class Verdicts(unittest.TestCase):
    def test_rules(self):
        self.assertEqual(ttl.verdict(0.08, 0.06, 0.10, "1h"), "5m")
        self.assertEqual(ttl.verdict(0.08, 0.06, 0.10, "5m"), "keep 5m")
        self.assertEqual(ttl.verdict(-0.12, -0.15, -0.10, "1h"), "keep 1h")
        self.assertEqual(ttl.verdict(0.03, -0.01, 0.07, "1h"), "either")      # interval crosses zero
        self.assertEqual(ttl.verdict(0.005, 0.002, 0.008, "1h"), "either")    # under a point

    def test_compare_end_to_end(self):
        rows = []
        for i in range(40):     # scripted runs: rapid, an hour apart
            rows += [req(f"s{i}", i * 2 * 3600 + k * 5, 1000 * k, 1000) for k in range(20)]
        r = ttl.compare(rows, P, 0.0, None, 200, random.Random(1))
        lane = r["lanes"]["headless"]
        self.assertEqual(lane["verdict"], "5m")
        self.assertEqual(lane["observed_ttl"], "1h")
        self.assertIn("scripted switch to 5m", ttl.short(r))
        self.assertLess(lane["reuse_ratio"], ttl.POLICY_BREAK_EVEN)


if __name__ == "__main__":
    unittest.main()
