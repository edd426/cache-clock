"""Regressions found on a Windows 11 laptop (2026-10-08). Run from the skill root:

    python3 -m unittest discover -s tests -p 'test_windows*.py'
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path, PureWindowsPath

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from cps_common import Coverage, Env  # noqa: E402
from adapters import vscode_copilot  # noqa: E402


class WinPath(PureWindowsPath):
    """Stands in for a Windows Path on any OS (as_posix gives forward slashes)."""


class Placeholder(unittest.TestCase):
    def test_windows_paths_use_forward_slashes_and_ignore_case(self):
        env = Env(home=WinPath("C:/Users/Someone"), appdata=WinPath("C:/Users/Someone/AppData/Roaming"),
                  localappdata=None, system="Windows", environ={})
        self.assertEqual(env.placeholder(WinPath(r"c:\users\someone\.copilot\session-store.db")),
                         "{HOME}/.copilot/session-store.db")
        self.assertEqual(env.placeholder(WinPath(r"C:\Users\Someone\AppData\Roaming\Code\User")),
                         "{APPDATA}/Code/User")

    def test_posix_paths_stay_case_sensitive(self):
        env = Env(home=Path("/home/someone"), appdata=None, localappdata=None, system="Linux", environ={})
        self.assertEqual(env.placeholder(Path("/home/someone/.codex")), "{HOME}/.codex")
        self.assertEqual(env.placeholder(Path("/HOME/someone/.codex")), "/HOME/someone/.codex")


class VSCodeLineSplit(unittest.TestCase):
    def test_unicode_line_separators_inside_json_strings(self):
        # json.dumps(ensure_ascii=False) leaves U+2028 / U+0085 unescaped; splitlines() would cut the record there
        rows = [{"kind": 0, "v": {"version": 3, "sessionId": "s1", "creationDate": 1_780_000_000_000,
                                  "requests": [], "customTitle": "a\u2028b\u0085c"}},
                {"kind": 1, "k": ["customTitle"], "v": "d\u2029e"}]          # two lines: a log, not one document
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s1.jsonl"
            p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
            cov = Coverage("vscode-copilot", True)
            vscode_copilot.read_session(p, cov)
            self.assertEqual(cov.parse_errors, 0)


if __name__ == "__main__":
    unittest.main()
