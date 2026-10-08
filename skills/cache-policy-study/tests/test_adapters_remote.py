"""Tests for the Gemini CLI, Antigravity and Cursor adapters and the sleep-log parsers, on SYNTHETIC fixtures
(no real user content; no subprocess calls). Run from the skill root:

    python3 -m unittest discover -s tests -p 'test_adapters_remote*.py'
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIX = HERE / "fixtures"
sys.path.insert(0, str(HERE.parent / "scripts"))

import power  # noqa: E402
from adapters import antigravity, cursor, gemini_cli  # noqa: E402
from cps_common import Coverage, Env, project_id  # noqa: E402


def utc(s: str) -> float:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp()


# ----------------------------------------------------------------------------- power

class TestPowerMac(unittest.TestCase):
    def setUp(self):
        self.asl = power.parse_asl_raw((FIX / "power" / "asl_raw.txt").read_text())

    def test_asl_records(self):
        kinds = [r[1] for r in self.asl]
        self.assertEqual(kinds.count("sleep"), 5)
        self.assertEqual(kinds.count("dark"), 2)
        self.assertEqual(kinds.count("wake"), 3)   # the Assertions record is ignored
        self.assertAlmostEqual(self.asl[0][0], 1790000000.5)

    def test_darkwake_does_not_end_sleep(self):
        out, open_ = power.pair(self.asl, mark_log_start=True)
        self.assertEqual([(i["start"], i["end"]) for i in out],
                         [(1790000010.0, 1790003600.25), (1790007200.0, 1790010000.0), (1790020000.0, 1790021000.0)])
        self.assertEqual([i["reason"] for i in out],
                         ["Maintenance Sleep (log start)", "Clamshell Sleep", "Clamshell Sleep (to DarkWake)"])
        self.assertEqual(open_, 1)   # the trailing Idle Sleep has no Wake yet

    def test_pmset_log(self):
        recs = power.parse_pmset_log((FIX / "power" / "pmset_log.txt").read_text())
        self.assertEqual([r[1] for r in recs], ["sleep", "dark", "sleep", "wake"])   # 'Wake Requests' excluded
        out, open_ = power.pair(recs)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["start"], utc("2026-09-21T16:00:00"))
        self.assertEqual(out[0]["end"], utc("2026-09-21T17:00:00"))
        self.assertEqual(out[0]["reason"], "Clamshell Sleep")
        self.assertEqual(open_, 0)

    def test_window_keeps_overlapping(self):
        out, _ = power.pair(self.asl)
        self.assertEqual(len(power.window(out, 1790003000, 1790008000)), 2)
        self.assertEqual(len(power.window(out, 1790011000, 1790019000)), 0)


class TestPowerWindows(unittest.TestCase):
    def test_wevtutil(self):
        direct, recs = power.parse_wevtutil_xml((FIX / "power" / "wevtutil.xml").read_text())
        self.assertEqual(len(direct), 1)
        self.assertAlmostEqual(direct[0]["start"], utc("2026-09-21T16:00:01.250000"))
        self.assertEqual([r[1] for r in recs], ["sleep", "wake", "sleep", "wake"])
        paired, open_ = power.pair(recs)
        merged = power.merge(direct + paired)
        self.assertEqual(len(merged), 2)
        self.assertAlmostEqual(merged[0]["start"], utc("2026-09-21T16:00:00.123456"), places=3)
        self.assertEqual(merged[0]["end"], utc("2026-09-21T17:00:05"))
        self.assertTrue(merged[1]["reason"].startswith("modern standby"))
        self.assertEqual(open_, 0)

    def test_garbage(self):
        self.assertEqual(power.parse_wevtutil_xml("<not xml"), ([], []))
        self.assertIsNone(power._win_time("yesterday"))


class TestPowerLinux(unittest.TestCase):
    def test_journal(self):
        recs = power.parse_journal_json((FIX / "power" / "journal.jsonl").read_text())
        out, open_ = power.pair(recs)
        self.assertEqual([(i["start"], i["end"]) for i in out], [(1790000000.0, 1790003600.0), (1790010000.0, 1790012000.0)])
        self.assertEqual(out[0]["reason"], "suspend")


class TestPowerNoCommand(unittest.TestCase):
    def test_unknown_os_is_empty(self):
        env = Env(home=Path("/nonexistent"), appdata=None, localappdata=None, system="Plan9", environ={})
        cov = Coverage("power", False)
        self.assertEqual(power.sleep_intervals(env, None, None, cov), [])
        self.assertTrue(cov.notes)


# ----------------------------------------------------------------------------- adapters: helpers

def mac_env(home: Path, **environ) -> Env:
    return Env(home=home, appdata=None, localappdata=None, system="Darwin", environ=dict(environ))


def run(mod, env: Env, since=None, until=None):
    cov = Coverage(mod.TOOL, mod.VERIFIED)
    return list(mod.collect(env, since, until, cov)), cov


def no_leaks(test: unittest.TestCase, evs, cov: Coverage, home: Path):
    """No raw paths, project names or message text in events or coverage."""
    blob = json.dumps(evs) + json.dumps(cov.__dict__)
    for bad in (str(home), "synthetic/project", "synthetic-proj", "/synthetic/ws", "synthetic prompt", "synthetic reply"):
        test.assertNotIn(bad, blob)


class TempHome(unittest.TestCase):
    fixture = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        if self.fixture:
            shutil.copytree(FIX / self.fixture / "home", self.home)
        else:
            self.home.mkdir()

    def tearDown(self):
        self.tmp.cleanup()


# ----------------------------------------------------------------------------- Gemini CLI

class TestGeminiCli(TempHome):
    fixture = "gemini-cli"

    def test_sessions(self):
        evs, cov = run(gemini_cli, mac_env(self.home))
        by = {}
        for e in evs:
            by.setdefault(e["session"], []).append(e)
        self.assertEqual(set(by), {"sess-main-0001", "sess-old-0002", "sess-logonly-0003"})
        main = by["sess-main-0001"]
        # m2 last copy wins; m3 info ignored; m4/m5 rewound away; m6 kept
        self.assertEqual([e["kind"] for e in main], ["prompt", "response", "prompt"])
        self.assertEqual(main[2]["t"], utc("2026-09-01T10:31:00"))
        self.assertEqual(main[1]["usage"], {"input": 200, "output": 70, "cache_read": 800, "cache_write": None,
                                            "cache_write_5m": None, "cache_write_1h": None, "ctx": 1070})
        self.assertEqual(main[1]["model"], "gemini-test-model")
        self.assertEqual(main[0]["project"], project_id("/synthetic/project"))
        self.assertEqual([e["kind"] for e in by["sess-old-0002"]], ["prompt", "response"])
        self.assertIsNone(by["sess-old-0002"][1]["usage"])
        self.assertEqual([e["kind"] for e in by["sess-logonly-0003"]], ["prompt", "prompt"])
        self.assertTrue(all(e["interactive"] is None and e["fidelity"] == "turn" for e in evs))
        self.assertEqual(cov.parse_errors, 1)
        self.assertTrue(any("subagent" in n for n in cov.notes))
        self.assertTrue(any("interactive unknown" in n for n in cov.notes))
        no_leaks(self, evs, cov, self.home)

    def test_window_and_env_override(self):
        evs, _ = run(gemini_cli, mac_env(self.home), since=utc("2026-08-15T00:00:00"))
        self.assertEqual({e["session"] for e in evs}, {"sess-main-0001"})
        evs, cov = run(gemini_cli, mac_env(Path("/nonexistent"), GEMINI_CLI_HOME=str(self.home)))
        self.assertEqual(len({e["session"] for e in evs}), 3)

    def test_absent(self):
        evs, cov = run(gemini_cli, mac_env(Path(self.tmp.name) / "nobody"))
        self.assertEqual((evs, cov.roots_found), ([], []))


# ----------------------------------------------------------------------------- Antigravity

def pb_varint(n: int) -> bytes:
    out = b""
    while True:
        b, n = n & 0x7F, n >> 7
        out += bytes([b | (0x80 if n else 0)])
        if not n:
            return out


def pb(num: int, v) -> bytes:
    if isinstance(v, int):
        return pb_varint(num << 3) + pb_varint(v)
    v = v.encode() if isinstance(v, str) else v
    return pb_varint(num << 3 | 2) + pb_varint(len(v)) + v


def pb_ts(t: float) -> bytes:
    return pb(1, int(t)) + pb(2, int(round((t - int(t)) * 1e9)))


U1, U2, U3 = ("11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222",
              "33333333-3333-4333-8333-333333333333")


class TestAntigravity(TempHome):
    fixture = "antigravity"

    def setUp(self):
        super().setUp()
        g = self.home / ".gemini"
        os.utime(g / "antigravity" / "conversations" / (U1 + ".pb"), (utc("2026-09-10T13:00:00"),) * 2)
        conv = g / "antigravity-cli" / "conversations"
        conv.mkdir(parents=True)
        self.t_call = utc("2026-09-12T08:00:05.250000")
        self.t_user = utc("2026-09-12T08:00:00")
        con = sqlite3.connect(str(conv / (U3 + ".db")))
        con.execute("CREATE TABLE gen_metadata (idx INTEGER, data BLOB)")
        con.execute("CREATE TABLE steps (idx INTEGER, step_type INTEGER, status INTEGER, step_payload BLOB, metadata BLOB)")
        usage_msg = pb(1, 7) + pb(2, 300) + pb(3, 120) + pb(4, 50) + pb(5, 9000) + pb(9, 80)
        chat = pb(4, usage_msg) + pb(9, pb(1, "x") + pb(4, pb_ts(self.t_call))) + pb(19, "test-model")
        con.execute("INSERT INTO gen_metadata VALUES (0, ?)", (pb(1, chat),))
        con.execute("INSERT INTO gen_metadata VALUES (1, ?)", (b"\xff\xff\xff",))   # malformed: skipped
        con.execute("INSERT INTO steps VALUES (0, 14, 3, ?, ?)", (pb(19, pb(2, "synthetic")), pb(1, pb_ts(self.t_user))))
        con.execute("INSERT INTO steps VALUES (1, 15, 3, x'', ?)", (pb(1, pb_ts(self.t_call)),))
        con.commit()
        con.close()
        os.utime(conv / (U3 + ".db"), (utc("2026-09-12T09:00:00"),) * 2)

    def test_sources(self):
        evs, cov = run(antigravity, mac_env(self.home))
        by = {}
        for e in evs:
            by.setdefault(e["session"], []).append(e)
        self.assertEqual(set(by), {U1, U2, U3})
        # .pb is encrypted: mtime + brain metadata updatedAt only
        self.assertEqual([(e["kind"], e["fidelity"]) for e in by[U1]], [("activity", "mtime")] * 2)
        self.assertEqual(sorted(e["t"] for e in by[U1]), [utc("2026-09-10T12:00:00"), utc("2026-09-10T13:00:00")])
        # transcript.jsonl (the backup copy of U2 is skipped as a duplicate)
        self.assertEqual([e["kind"] for e in by[U2]], ["prompt", "response", "auto_prompt", "response"])
        self.assertEqual(by[U2][0]["t"], utc("2026-09-11T09:00:00"))
        # SQLite .db, protobuf decoded
        self.assertEqual([e["kind"] for e in by[U3]], ["prompt", "response"])
        self.assertEqual(by[U3][0]["t"], self.t_user)
        self.assertAlmostEqual(by[U3][1]["t"], self.t_call, places=3)
        self.assertEqual(by[U3][1]["model"], "test-model")
        self.assertEqual(by[U3][1]["usage"], {"input": 300, "output": 120, "cache_read": 9000, "cache_write": 50,
                                              "cache_write_5m": None, "cache_write_1h": None, "ctx": 9470})
        self.assertEqual(cov.files_skipped.get("duplicate-conversation"), 1)
        self.assertTrue(any("implicit" in n for n in cov.notes))
        self.assertTrue(any("db=1" in n and "mtime=1" in n and "transcript=1" in n for n in cov.notes))
        no_leaks(self, evs, cov, self.home)

    def test_implausible_db_times_rejected(self):
        db = self.home / ".gemini" / "antigravity-cli" / "conversations" / (U3 + ".db")
        os.utime(db, (utc("2026-01-01T00:00:00"),) * 2)   # decoded times are after the file's mtime
        evs, cov = run(antigravity, mac_env(self.home))
        self.assertFalse([e for e in evs if e["session"] == U3])
        self.assertTrue(any("implausible" in n for n in cov.notes))

    def test_proto_time_forms(self):
        self.assertEqual(antigravity.proto_time(1790000000), 1790000000.0)
        self.assertEqual(antigravity.proto_time(1790000000123), 1790000000.123)
        self.assertEqual(antigravity.proto_time(b"2026-09-12T08:00:00Z"), utc("2026-09-12T08:00:00"))
        self.assertIsNone(antigravity.proto_time(b"\xff\xfe"))
        self.assertEqual(antigravity.fields(b"\x08"), [])   # truncated varint

    def test_absent(self):
        evs, cov = run(antigravity, mac_env(Path(self.tmp.name) / "nobody"))
        self.assertEqual((evs, cov.roots_found), ([], []))


# ----------------------------------------------------------------------------- Cursor

def kv(con, table, key, value):
    con.execute(f"INSERT INTO {table} VALUES (?, ?)", (key, value if isinstance(value, str) else json.dumps(value)))


def ms(s: str) -> int:
    return int(utc(s) * 1000)


class TestCursor(TempHome):
    def setUp(self):
        super().setUp()
        user = self.home / "Library" / "Application Support" / "Cursor" / "User"
        (user / "globalStorage").mkdir(parents=True)
        ws = user / "workspaceStorage" / "0123456789abcdef0123456789abcdef"
        ws.mkdir(parents=True)
        (ws / "workspace.json").write_text(json.dumps({"folder": "file:///synthetic/ws2"}))
        con = sqlite3.connect(str(ws / "state.vscdb"))
        con.execute("CREATE TABLE ItemTable (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB)")
        kv(con, "ItemTable", "composer.composerData", {"allComposers": [
            {"composerId": "C2", "name": "synthetic", "createdAt": ms("2026-09-02T10:00:00"), "lastUpdatedAt": ms("2026-09-02T11:00:00")},
            {"composerId": "C5", "name": "synthetic", "createdAt": ms("2026-09-05T10:00:00"), "lastUpdatedAt": ms("2026-09-05T10:20:00")}],
            "selectedComposerIds": []})
        kv(con, "ItemTable", "aiService.generations", [{"unixMs": ms("2026-06-01T08:00:00"), "type": "composer", "textDescription": "synthetic"},
                                                       {"unixMs": ms("2026-06-01T08:05:00"), "type": "apply"}])
        con.commit()
        con.close()
        con = sqlite3.connect(str(user / "globalStorage" / "state.vscdb"))
        con.execute("CREATE TABLE ItemTable (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB)")
        con.execute("CREATE TABLE cursorDiskKV (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB)")
        kv(con, "cursorDiskKV", "composerData:C1", {"_v": 16, "composerId": "C1", "name": "synthetic prompt",
            "createdAt": ms("2026-09-01T09:00:00"), "lastUpdatedAt": ms("2026-09-01T09:10:00"),
            "modelConfig": {"modelName": "composer-model"}, "workspaceIdentifier": {"id": "w1", "uri": {"fsPath": "/synthetic/ws1"}},
            "fullConversationHeadersOnly": [{"bubbleId": "b1", "type": 1}, {"bubbleId": "b2", "type": 2}, {"bubbleId": "b3", "type": 2}]})
        kv(con, "cursorDiskKV", "bubbleId:C1:b1", {"_v": 3, "type": 1, "text": "synthetic prompt", "createdAt": "2026-09-01T09:00:30.000Z"})
        kv(con, "cursorDiskKV", "bubbleId:C1:b2", {"_v": 3, "type": 2, "text": "synthetic reply", "tokenCount": {"inputTokens": 0, "outputTokens": 0},
            "timingInfo": {"clientStartTime": 654927, "clientRpcSendTime": ms("2026-09-01T09:00:31"), "clientEndTime": ms("2026-09-01T09:00:40")}})
        kv(con, "cursorDiskKV", "bubbleId:C1:b3", {"_v": 3, "type": 2, "createdAt": "2026-09-01T09:00:45.000Z",
            "tokenCount": {"inputTokens": 100, "outputTokens": 20}, "modelInfo": {"modelName": "bubble-model"}})
        kv(con, "cursorDiskKV", "composerData:C2", {"_v": 16, "createdAt": ms("2026-09-02T10:00:00"), "lastUpdatedAt": ms("2026-09-02T11:00:00")})
        kv(con, "cursorDiskKV", "bubbleId:C2:b1", {"_v": 3, "type": 1, "text": "synthetic prompt"})
        kv(con, "cursorDiskKV", "composerData:C3", {"_v": 16, "createdAt": ms("2026-09-03T10:00:00"), "lastUpdatedAt": ms("2026-09-03T10:00:00")})
        kv(con, "cursorDiskKV", "composerData:C4", {"_v": 2, "createdAt": ms("2025-03-01T10:00:00"), "lastUpdatedAt": ms("2025-03-01T10:05:00"),
            "trackedGitRepos": [{"repoPath": "/synthetic/ws4"}],
            "conversation": [{"type": 1, "text": "synthetic prompt", "timingInfo": {"clientRpcSendTime": ms("2025-03-01T10:00:10")}},
                             {"type": 2, "text": "synthetic reply", "timingInfo": {"clientSettleTime": ms("2025-03-01T10:00:20")}}]})
        kv(con, "cursorDiskKV", "composerData:bad", "not json")
        kv(con, "cursorDiskKV", "checkpointId:C1:x", {"synthetic": True})
        con.commit()
        con.close()

    def sessions(self, evs):
        by = {}
        for e in evs:
            by.setdefault(e["session"], []).append(e)
        return by

    def test_cursor(self):
        evs, cov = run(cursor, mac_env(self.home))
        by = self.sessions(evs)
        self.assertEqual(set(by), {"C1", "C2", "C4", "C5", "aiService:" + project_id("0123456789abcdef0123456789abcdef")})
        c1 = by["C1"]
        self.assertEqual([e["kind"] for e in c1], ["prompt", "response", "response"])
        self.assertEqual(c1[1]["t"], utc("2026-09-01T09:00:31"))   # clientStartTime is not an epoch
        self.assertIsNone(c1[1]["usage"])                          # zero token counts are "unknown"
        self.assertEqual((c1[2]["usage"]["input"], c1[2]["usage"]["output"]), (100, 20))
        self.assertEqual((c1[1]["model"], c1[2]["model"]), ("composer-model", "bubble-model"))
        self.assertEqual(c1[0]["project"], project_id("/synthetic/ws1"))
        # bubbles without times -> session bounds; project from the workspace DB
        self.assertEqual([(e["kind"], e["fidelity"]) for e in by["C2"]], [("session_start", "session"), ("session_end", "session")])
        self.assertEqual(by["C2"][0]["project"], project_id("/synthetic/ws2"))
        self.assertEqual([e["kind"] for e in by["C4"]], ["prompt", "response"])   # inline conversation (_v 2)
        self.assertEqual(by["C4"][0]["project"], project_id("/synthetic/ws4"))
        self.assertEqual([e["kind"] for e in by["C5"]], ["session_start", "session_end"])   # workspace-only
        gen = by["aiService:" + project_id("0123456789abcdef0123456789abcdef")]
        self.assertEqual([e["kind"] for e in gen], ["activity", "activity"])
        self.assertTrue(any("1 empty composers" in n for n in cov.notes))
        no_leaks(self, evs, cov, self.home)

    def test_python_fallback_matches_sql(self):
        db = self.home / "Library" / "Application Support" / "Cursor" / "User" / "globalStorage" / "state.vscdb"
        con = sqlite3.connect(str(db))

        class NoJson:
            def execute(self, sql, args=()):
                if "json_" in sql:
                    raise sqlite3.OperationalError("no such function: json_extract")
                return con.execute(sql, args)

        for each in (None, "conversation"):
            a = cursor.json_rows(con, "cursorDiskKV", "key >= ? AND key < ?", ("composerData:", "composerData;"),
                                 cursor.BUBBLE_FIELDS if each else cursor.COMPOSER_FIELDS, each=each)
            b = cursor.json_rows(NoJson(), "cursorDiskKV", "key >= ? AND key < ?", ("composerData:", "composerData;"),
                                 cursor.BUBBLE_FIELDS if each else cursor.COMPOSER_FIELDS, each=each)
            self.assertEqual(sorted(a, key=repr), sorted(b, key=repr))
        con.close()

    def test_locked_wal_db_is_read(self):
        db = Path(self.tmp.name) / "wal.vscdb"
        writer = sqlite3.connect(str(db))
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE t (x)")
        writer.execute("INSERT INTO t VALUES (1)")
        writer.commit()   # left in the -wal: the writer stays open, no checkpoint
        cov = Coverage("cursor", False)
        with cursor.open_ro(db, cov) as con:
            self.assertEqual(con.execute("SELECT x FROM t").fetchall(), [(1,)])
        writer.close()

    def test_folder_path(self):
        self.assertEqual(cursor.folder_path("file:///c%3A/Users/x/proj"), "c:/Users/x/proj")
        self.assertEqual(cursor.folder_path("file:///home/x/my%20proj"), "/home/x/my proj")
        self.assertTrue(cursor.folder_path("vscode-remote://ssh-remote%2Bhost/x").startswith("vscode-remote:"))

    def test_absent(self):
        evs, cov = run(cursor, mac_env(Path(self.tmp.name) / "nobody"))
        self.assertEqual((evs, cov.roots_found), ([], []))


if __name__ == "__main__":
    unittest.main()
