"""Claude Code adapter: response dedupe, prompt classification, turn_end, compaction, headless flag."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from cps_common import Coverage, Env  # noqa: E402
from adapters import claude_code  # noqa: E402


def row(**kw):
    base = {"sessionId": "s1", "cwd": "/placeholder/project", "entrypoint": "cli"}
    base.update(kw)
    return base


def user(ts, content, **kw):
    return row(type="user", timestamp=ts, message={"role": "user", "content": content}, **kw)


def assistant(ts, mid, read=1000, write=200):
    return row(type="assistant", timestamp=ts, message={"id": mid, "model": "claude-x", "usage": {
        "input_tokens": 5, "output_tokens": 50, "cache_read_input_tokens": read, "cache_creation_input_tokens": write,
        "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": write}}})


class ClaudeCodeAdapter(unittest.TestCase):
    def collect(self, rows, entrypoint="cli"):
        with tempfile.TemporaryDirectory() as home:
            p = Path(home) / ".claude" / "projects" / "-placeholder"
            p.mkdir(parents=True)
            with open(p / "s1.jsonl", "w") as f:
                for r in rows:
                    r["entrypoint"] = entrypoint
                    f.write(json.dumps(r) + "\n")
            env = Env.detect(home)
            env.environ.pop("CLAUDE_CONFIG_DIR", None)
            return list(claude_code.collect(env, None, None, Coverage("claude-code", True)))

    def test_classification(self):
        evs = self.collect([
            user("2026-10-01T10:00:00Z", "a typed question"),
            assistant("2026-10-01T10:00:05Z", "m1"),
            assistant("2026-10-01T10:00:06Z", "m1"),                       # second content block: same response
            row(type="system", subtype="turn_duration", timestamp="2026-10-01T10:00:07Z"),
            user("2026-10-01T10:30:00Z", "<command-name>/compact</command-name>"),
            user("2026-10-01T10:31:00Z", "This session is being continued", isCompactSummary=True),
            row(type="system", subtype="compact_boundary", timestamp="2026-10-01T10:31:00Z",
                compactMetadata={"trigger": "manual", "preTokens": 300000, "postTokens": 9000}),
            user("2026-10-01T11:00:00Z", "Another Claude session sent a message", isMeta=True),
            user("2026-10-01T11:00:01Z", "<task-notification> done"),
            user("2026-10-01T11:00:02Z", "<local-command-stdout>output</local-command-stdout>"),
            user("2026-10-01T11:00:03Z", [{"type": "tool_result", "content": "x"}]),
            user("2026-10-01T11:00:04Z", "an expanded skill body", isMeta=True),
        ])
        kinds = [e["kind"] for e in evs]
        self.assertEqual(kinds, ["prompt", "response", "turn_end", "prompt", "compaction", "auto_prompt", "auto_prompt"])
        resp = evs[1]
        self.assertEqual(resp["usage"]["ctx"], 5 + 1000 + 200 + 50)
        self.assertEqual(resp["usage"]["cache_write_1h"], 200)
        self.assertTrue(all(e["interactive"] is True for e in evs))
        self.assertNotIn("/placeholder", json.dumps(evs))

    def test_prompt_source(self):
        evs = self.collect([
            user("2026-10-01T10:00:00Z", "Check sweeps and report", isMeta=True, promptSource="system"),
            user("2026-10-01T10:05:00Z", "a queued question", promptSource="queued"),
            user("2026-10-01T10:06:00Z", "<local-command-stdout>x</local-command-stdout>", promptSource="typed"),
        ])
        self.assertEqual([e["kind"] for e in evs], ["auto_prompt", "prompt"])

    def test_subagents_feed_spend_not_behaviour(self):
        import collect
        with tempfile.TemporaryDirectory() as home:
            p = Path(home) / ".claude" / "projects" / "-placeholder"
            (p / "s1" / "subagents").mkdir(parents=True)
            with open(p / "s1.jsonl", "w") as f:
                for r in (user("2026-10-01T10:00:00Z", "q"), assistant("2026-10-01T10:00:05Z", "m1")):
                    f.write(json.dumps(r) + "\n")
            with open(p / "s1" / "subagents" / "agent-a1.jsonl", "w") as f:
                for r in (user("2026-10-01T10:00:06Z", "sub task", isSidechain=True, agentId="a1"),
                          assistant("2026-10-01T10:00:08Z", "m2", read=40, write=10),
                          assistant("2026-10-01T10:00:09Z", "m2", read=40, write=10)):   # second block, same response
                    r.update(isSidechain=True, agentId="a1")
                    f.write(json.dumps(r) + "\n")
            out = Path(home) / "out"
            env = Env.detect(home)
            env.environ.pop("CLAUDE_CONFIG_DIR", None)
            evs = list(claude_code.collect(env, None, None, Coverage("claude-code", True)))
            self.assertEqual([(e["kind"], e["subagent"]) for e in evs], [("prompt", False), ("response", False), ("response", True)])
            self.assertEqual(collect.main(["--out", str(out), "--home", home, "--only", "claude-code", "--no-power"]), 0)
            kept = [json.loads(x) for x in (out / "events.jsonl").read_text().splitlines()]
            self.assertFalse(any(e["subagent"] for e in kept))
            rows = json.loads((out / "spend.json").read_text())["rows"]
            by = {r["class"]: r for r in rows}
            self.assertEqual(by["main"]["cache_read"], 1000)
            self.assertEqual((by["subagent"]["responses"], by["subagent"]["cache_read"], by["subagent"]["cache_write_1h"]), (1, 40, 10))
            ttl_rows = [json.loads(x) for x in (out / "ttl.jsonl").read_text().splitlines()]
            self.assertEqual([(r["class"], r["lane"]) for r in ttl_rows], [("main", "s1"), ("subagent", "s1/agent-a1")])

    def test_headless(self):
        evs = self.collect([user("2026-10-01T10:00:00Z", "q"), assistant("2026-10-01T10:00:05Z", "m1")], "sdk-cli")
        self.assertTrue(all(e["interactive"] is False for e in evs))


if __name__ == "__main__":
    unittest.main()
