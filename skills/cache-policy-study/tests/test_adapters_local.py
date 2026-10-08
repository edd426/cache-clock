"""Synthetic-fixture tests for the codex, vscode-copilot and copilot-cli adapters (stdlib unittest).

    cd <skill root> && python3 -m unittest discover -s tests -p 'test_adapters_local*.py'

Every fixture under tests/fixtures/<tool>/ is invented placeholder data laid out as a home folder; each test
copies one into a temp folder and runs the adapter through collect(Env(home=...), ...).
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "scripts"))

from cps_common import Coverage, Env, parse_time, project_id  # noqa: E402
from adapters import codex, copilot_cli, vscode_copilot  # noqa: E402

FIXTURES = HERE / "fixtures"


def run(mod, home: Path, since=None, until=None):
    env = Env(home=home, appdata=None, localappdata=None, system="Darwin", environ={})
    cov = Coverage(mod.TOOL, mod.VERIFIED)
    return list(mod.collect(env, since, until, cov)), cov


class FixtureHome(unittest.TestCase):
    tool = ""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name) / "home"
        shutil.copytree(FIXTURES / self.tool, self.home)

    def tearDown(self):
        self._tmp.cleanup()

    def by_session(self, events):
        out = {}
        for e in events:
            out.setdefault(e["session"], []).append(e)
        return out


class CodexTest(FixtureHome):
    tool = "codex"
    A = "00000000-0000-4000-8000-00000000000a"
    B = "00000000-0000-4000-8000-00000000000b"
    T0 = parse_time("2026-01-02T10:00:00Z")

    def test_interactive_vs_exec_and_subagent_skip(self):
        events, cov = run(codex, self.home)
        s = self.by_session(events)
        self.assertEqual(set(s), {self.A, self.B})          # the guardian subagent rollout is skipped
        self.assertTrue(all(e["interactive"] is True for e in s[self.A]))
        self.assertTrue(all(e["interactive"] is False for e in s[self.B]))
        self.assertTrue(any("1 subagent rollouts skipped" in n for n in cov.notes))
        self.assertEqual(s[self.A][0]["project"], project_id("/placeholder/project-one"))

    def test_prompts_responses_dedupe_and_mapping(self):
        s = self.by_session(run(codex, self.home)[0])
        a = [(e["kind"], round(e["t"] - self.T0)) for e in s[self.A]]
        self.assertEqual(a, [("prompt", 2), ("response", 10), ("prompt", 11), ("response", 20),
                             ("compaction", 21), ("response", 30)])
        r1 = s[self.A][1]
        self.assertEqual(r1["usage"]["input"], 200)          # 1000 input incl. 800 cached
        self.assertEqual(r1["usage"]["cache_read"], 800)
        self.assertEqual(r1["usage"]["cache_write"], 0)
        self.assertEqual(r1["usage"]["output"], 50)
        self.assertEqual(r1["usage"]["ctx"], 1050)
        self.assertEqual(r1["model"], "model-x")
        comp = s[self.A][4]["compaction"]
        self.assertEqual(comp, {"pre": 1260, "post": None, "trigger": "auto"})

    def test_legacy_token_count_dedupe(self):
        s = self.by_session(run(codex, self.home)[0])
        b = [(e["kind"], round(e["t"] - self.T0)) for e in s[self.B]]
        self.assertEqual(b, [("prompt", 2), ("response", 5), ("response", 9)])   # repeat at 6s dropped
        self.assertEqual(s[self.B][2]["usage"]["input"], 200)
        self.assertEqual(s[self.B][2]["usage"]["cache_read"], 400)
        self.assertEqual(s[self.B][2]["usage"]["ctx"], 615)

    def test_window_and_missing_store(self):
        events, _ = run(codex, self.home, since=self.T0 + 15, until=self.T0 + 25)
        self.assertEqual(sorted(e["kind"] for e in events), ["compaction", "response"])
        events, cov = run(codex, Path(self._tmp.name) / "nowhere")
        self.assertEqual(events, [])
        self.assertEqual(cov.roots_found, [])


class VSCodeTest(FixtureHome):
    tool = "vscode-copilot"
    T0 = 1767348000.0

    def test_both_formats_read(self):
        events, cov = run(vscode_copilot, self.home)
        s = self.by_session(events)
        self.assertEqual(set(s), {"vs-old", "vs-new"})
        self.assertEqual(cov.files_skipped.get("session with no requests"), 1)
        self.assertEqual(cov.files_skipped.get("imported session (synthetic timestamps)"), 1)
        self.assertTrue(any(".json 3" in n and ".jsonl 1" in n for n in cov.notes))
        self.assertTrue(all(e["interactive"] is True and e["usage"] is None for e in events))

    def test_old_json(self):
        s = self.by_session(run(vscode_copilot, self.home)[0])
        old = [(e["kind"], round(e["t"] - self.T0)) for e in s["vs-old"]]
        self.assertEqual(old, [("prompt", 5), ("response", 9), ("prompt", 60)])  # cancelled: no response
        self.assertEqual(s["vs-old"][0]["project"], project_id("wshashold"))
        self.assertEqual(s["vs-old"][0]["model"], "model-v")

    def test_jsonl_mutation_log(self):
        s = self.by_session(run(vscode_copilot, self.home)[0])
        new = [(e["kind"], round(e["t"] - self.T0)) for e in s["vs-new"]]
        # n3 is truncated away by the push with i=2; n4 is cancelled (value 2) so it has no response
        self.assertEqual(new, [("prompt", 210), ("response", 230), ("auto_prompt", 300), ("response", 305),
                               ("prompt", 500)])

    def test_replay_rules(self):
        state = vscode_copilot.replay([
            '{"kind":0,"v":{"a":{"b":[1,2,3]},"c":1}}',
            '{"kind":2,"k":["a","b"],"v":[9],"i":1}',
            '{"kind":1,"k":["c"],"v":5}',
            '{"kind":3,"k":["a"]}',
            'not json',
        ])
        self.assertEqual(state, {"c": 5})
        state = vscode_copilot.replay(['{"kind":0,"v":{"a":{"b":[1,2,3]}}}', '{"kind":2,"k":["a","b"],"v":[9],"i":1}'])
        self.assertEqual(state["a"]["b"], [1, 9])


class CopilotCLITest(FixtureHome):
    tool = "copilot-cli"
    T0 = parse_time("2026-01-02T11:00:00Z")

    def make_store(self):
        db = self.home / ".copilot" / "session-store.db"
        con = sqlite3.connect(str(db))
        con.executescript((self.home / "session-store.sql").read_text())
        con.commit()
        con.close()

    def test_event_log_without_store(self):
        events, cov = run(copilot_cli, self.home)
        s = self.by_session(events)
        a = [(e["kind"], round(e["t"] - self.T0), e["subagent"]) for e in s["sess-a"]]
        self.assertEqual(a, [("prompt", 5, False), ("auto_prompt", 20, False), ("response", 9, False),
                             ("response", 12, True), ("response", 25, False), ("compaction", 30, False)])
        comp = s["sess-a"][-1]["compaction"]
        self.assertEqual(comp, {"pre": 9000, "post": 1500, "trigger": None})
        self.assertTrue(all(e["usage"] is None for e in s["sess-a"]))          # assistant.usage is ephemeral
        self.assertEqual(s["sess-a"][0]["project"], project_id("/placeholder/project-three"))
        self.assertTrue(all(e["interactive"] is None for e in events))
        self.assertTrue(any("process logs not read" in n for n in cov.notes))

    def test_legacy_json_documents(self):
        s = self.by_session(run(copilot_cli, self.home)[0])
        legacy = [(e["kind"], round(e["t"] - self.T0)) for e in s["legacy-1"]]
        self.assertEqual(legacy, [("prompt", 101), ("response", 105)])
        self.assertEqual(s["legacy-1"][1]["usage"]["ctx"], 44)
        self.assertEqual([(e["kind"], e["fidelity"]) for e in s["legacy-2"]], [("session_start", "session")])

    def test_session_store(self):
        self.make_store()
        events, cov = run(copilot_cli, self.home)
        self.assertIn("{HOME}/.copilot/session-store.db", cov.roots_found)
        s = self.by_session(events)
        resp = [e for e in s["sess-a"] if e["kind"] == "response"]
        self.assertEqual(len(resp), 2)                       # store rows replace the events.jsonl responses
        main = [e for e in resp if not e["subagent"]][0]
        self.assertEqual(main["usage"], {"input": 500, "output": 100, "cache_read": 4000, "cache_write": 500,
                                         "cache_write_5m": None, "cache_write_1h": None, "ctx": 5100})
        self.assertEqual(sum(1 for e in s["sess-a"] if e["kind"] == "prompt"), 1)   # no store-turn duplicate
        b = [(e["kind"], round(e["t"] - self.T0)) for e in s["sess-b"]]
        self.assertEqual(b, [("prompt", 3600), ("response", 3630)])                # NULL user_message skipped
        self.assertEqual(s["sess-b"][1]["usage"]["cache_write"], 2000)
        self.assertEqual(s["sess-b"][0]["project"], project_id("/placeholder/project-four"))

    def test_absent_store(self):
        events, cov = run(copilot_cli, Path(self._tmp.name) / "nowhere")
        self.assertEqual(events, [])
        self.assertEqual(cov.roots_found, [])


if __name__ == "__main__":
    unittest.main()
