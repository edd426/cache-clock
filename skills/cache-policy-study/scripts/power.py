"""OS sleep/wake log -> intervals when the machine was asleep (user processes frozen).

    sleep_intervals(env, since, until, cov) -> [{"start": epoch_s, "end": epoch_s, "reason": str}, ...]

Rule: asleep from the first Sleep after a full Wake until the next full (user) Wake. Maintenance wakes
(macOS DarkWake) do NOT end a sleep. An interval is returned whole when it overlaps [since, until); a sleep
still open at the end of the log is dropped (its Wake is not logged yet) and noted on `cov`.

macOS (verified 2026-10-07, Darwin 27, MacBook Air): the powerd archive /private/var/log/powermanagement/
YYYY.MM.DD.asl, read with `syslog -F raw -f <file> -k com.apple.iokit.domain Req '^(Sleep|Wake|DarkWake)$'`
(records carry `[Time <epoch>]` and `[com.apple.iokit.domain Sleep|Wake|DarkWake]`), merged with
`pmset -g log` (same store, shorter span, local-time lines "YYYY-MM-DD HH:MM:SS +zzzz Type\\tMessage").
The interval's reason is the first Sleep's "Entering Sleep state due to '<reason>'" ('Clamshell Sleep',
'Idle Sleep', ...); the Maintenance Sleeps after DarkWakes stay inside it. Reference check on this Mac,
2026-09-23..10-07: 1584 Sleep, 1568 DarkWake, 19 Wake records -> 19 intervals.

Windows (UNVERIFIED — no Windows machine here): `wevtutil qe System /q:<XPath> /f:xml` for
  - Microsoft-Windows-Power-Troubleshooter EventID 1 "The system has returned from a low power state",
    EventData SleepTime / WakeTime (UTC, 7 fractional digits) -> one interval directly;
  - Microsoft-Windows-Kernel-Power 42 (entering sleep) -> next 107 (resumed from sleep);
  - Microsoft-Windows-Kernel-Power 506 (entering Modern Standby) -> next 507 (exiting Modern Standby).
  Overlapping intervals from the three sources are merged. Sources:
  https://itprotoday.com/it-infrastructure/q-my-machine-keeps-coming-out-of-sleep-how-can-i-find-out-why-
  https://www.elevenforum.com/t/find-wake-source-for-windows-11-computer.7007/post-137580
  https://learn.microsoft.com/en-us/answers/questions/4349965/windows-11-wont-stay-asleep-have-already-done-a-lo
  https://www.elevenforum.com/t/how-to-find-what-program-is-using-setthreadexecutionstate-to-cause-exit-from-modern-standby.24346/page-2
  Check first on a real laptop: the System log is size-capped (often only weeks of history) — compare the
  first interval's date with the study window; on a Modern Standby laptop expect 506/507 pairs, not 42/107.
  Modern Standby is counted as asleep (desktop apps are paused by the Desktop Activity Moderator).

Linux (UNVERIFIED): `journalctl -o json --no-pager _COMM=systemd-sleep`; systemd-sleep logs MESSAGE_ID
6bbd95ee977941e497c48be27c254128 (sleep start, field SLEEP=suspend|hibernate|...) and
8811e6df2a8e40f58a94cea26f8ebf14 (sleep stop); older text "Entering sleep state"/"Suspending system"
and "System returned from sleep"/"System resumed" are the fallback. Source: systemd src/systemd/sd-messages.h
(SD_MESSAGE_SLEEP_START / SD_MESSAGE_SLEEP_STOP), https://github.com/systemd/systemd/blob/main/src/systemd/sd-messages.h
"""
from __future__ import annotations

import json
import platform
import re
import subprocess
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from cps_common import Coverage, Env

VERIFIED = platform.system() == "Darwin"

ASL_DIR = Path("/private/var/log/powermanagement")
TIMEOUT = 60

# A record is (t, kind, reason) with kind in {'sleep', 'dark', 'wake'}.
Record = Tuple[float, str, str]


def _run(cmd: List[str], cov: Coverage, timeout: int = TIMEOUT) -> Optional[str]:
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=timeout)
    except FileNotFoundError:
        cov.note(f"{cmd[0]} not found")
        return None
    except subprocess.TimeoutExpired:
        cov.note(f"{cmd[0]} timed out after {timeout}s")
        return None
    except OSError as exc:
        cov.note(f"{cmd[0]} failed: {type(exc).__name__}")
        return None
    if p.returncode != 0 and not p.stdout:
        cov.note(f"{cmd[0]} exited {p.returncode} (missing permission?)")
        return None
    return p.stdout.decode("utf-8", "replace")


# ---------------------------------------------------------------- pairing (all platforms)

def pair(records: Iterable[Record], mark_log_start: bool = False) -> Tuple[List[Dict], int]:
    """Sleep after a full Wake opens an interval; DarkWake is ignored; the next full Wake closes it.

    Returns (intervals, open) where open is 1 if the log ends asleep. An interval opened before any Wake was
    seen is labelled '(log start)' when mark_log_start (macOS, where a Sleep also follows every DarkWake):
    the log may have begun mid-sleep, so its true start can be earlier."""
    out: List[Dict] = []
    start, reason, woke = None, "", False
    for t, kind, why in sorted(records, key=lambda r: r[0]):
        if kind == "sleep" and start is None:
            # before the first Wake we cannot tell whether this Sleep follows a full Wake or a DarkWake:
            # the real start may be earlier than the log
            start, reason = t, (why or "sleep") + ("" if woke or not mark_log_start else " (log start)")
        elif kind == "wake":
            woke = True
        if kind == "wake" and start is not None:
            if t > start:
                out.append({"start": start, "end": t, "reason": reason or "sleep"})
            start, reason = None, ""
    return out, int(start is not None)


def merge(intervals: List[Dict]) -> List[Dict]:
    """Union of overlapping intervals (the earliest one's reason wins)."""
    out: List[Dict] = []
    for iv in sorted(intervals, key=lambda i: i["start"]):
        if out and iv["start"] <= out[-1]["end"]:
            out[-1]["end"] = max(out[-1]["end"], iv["end"])
        else:
            out.append(dict(iv))
    return out


def window(intervals: List[Dict], since: Optional[float], until: Optional[float]) -> List[Dict]:
    return [iv for iv in intervals if (since is None or iv["end"] > since) and (until is None or iv["start"] < until)]


# ---------------------------------------------------------------- macOS

_ASL_TIME = re.compile(r"\[Time (\d+)\]")
_ASL_NANO = re.compile(r"\[TimeNanoSec (\d+)\]")
_ASL_DOMAIN = re.compile(r"\[com\.apple\.iokit\.domain (Sleep|Wake|DarkWake)\]")
_REASON = re.compile(r"Entering (Sleep|DarkWake) state due to '([^']*)'")
_PMSET_LINE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d [+-]\d{4}) (\S[^\t]*?)\s*\t(.*)$")
_KIND = {"Sleep": "sleep", "Wake": "wake", "DarkWake": "dark"}


def _reason(text: str) -> str:
    """'Clamshell Sleep'; a lid close that only drops to DarkWake (an assertion such as caffeinate held the
    system up) is labelled '... (to DarkWake)' — still counted as asleep, but separable downstream."""
    m = _REASON.search(text)
    if not m:
        return ""
    return m.group(2) + (" (to DarkWake)" if m.group(1) == "DarkWake" else "")


def parse_asl_raw(text: str) -> List[Record]:
    """Lines of `syslog -F raw` output -> records. Messages contain nested brackets, so fields are matched,
    not split."""
    recs = []
    for line in text.splitlines():
        d = _ASL_DOMAIN.search(line)
        t = _ASL_TIME.search(line)
        if not d or not t:
            continue
        ns = _ASL_NANO.search(line)
        recs.append((int(t.group(1)) + (int(ns.group(1)) / 1e9 if ns else 0.0), _KIND[d.group(1)], _reason(line)))
    return recs


def parse_pmset_log(text: str) -> List[Record]:
    recs = []
    for line in text.splitlines():
        m = _PMSET_LINE.match(line)
        if not m or m.group(2) not in _KIND:
            continue
        try:
            t = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S %z").timestamp()
        except ValueError:
            continue
        recs.append((t, _KIND[m.group(2)], _reason(m.group(3))))
    return recs


def _asl_files(since: Optional[float], until: Optional[float]) -> List[Path]:
    files = []
    for p in sorted(ASL_DIR.glob("*.asl")):
        try:
            day = datetime.strptime(p.stem, "%Y.%m.%d").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        # one day of slack each side: a sleep can start before the window and the file date is local
        if since is not None and (day + timedelta(days=2)).timestamp() < since:
            continue
        if until is not None and (day - timedelta(days=1)).timestamp() > until:
            continue
        files.append(p)
    return files


def _macos(env: Env, since, until, cov: Coverage) -> List[Record]:
    recs: List[Record] = []
    cov.roots_checked.append(str(ASL_DIR))
    if ASL_DIR.is_dir():
        files = _asl_files(None if since is None else since - 30 * 86400, until)
        n = 0
        for f in files:
            text = _run(["syslog", "-F", "raw", "-f", str(f), "-k", "com.apple.iokit.domain", "Req",
                         "^(Sleep|Wake|DarkWake)$"], cov)
            if text is None:
                cov.skip("asl-unreadable")
                continue
            cov.files_read += 1
            got = parse_asl_raw(text)
            n += len(got)
            recs.extend(got)
        if n:
            cov.roots_found.append(str(ASL_DIR))
    cov.roots_checked.append("pmset -g log")
    text = _run(["pmset", "-g", "log"], cov, timeout=120)
    if text:
        got = parse_pmset_log(text)
        if got:
            cov.roots_found.append("pmset -g log")
            recs.extend(got)
    # the two sources overlap: keep one record per (kind, whole second), the ASL copy (sub-second) first
    seen, out = set(), []
    for r in recs:
        key = (r[1], int(r[0]))
        if key not in seen:
            seen.add(key)
            out.append(r)
    if out:
        cov.first_record = min(r[0] for r in out)  # type: ignore[attr-defined]
        counts = {k: sum(1 for r in out if r[1] == k) for k in ("sleep", "dark", "wake")}
        cov.note(f"macOS records: {counts['sleep']} Sleep, {counts['dark']} DarkWake, {counts['wake']} Wake")
    return out


# ---------------------------------------------------------------- Windows

_EVT_NS = "{http://schemas.microsoft.com/win/2004/08/events/event}"
_WIN_QUERY = ("*[System[(Provider[@Name='Microsoft-Windows-Kernel-Power'] and "
              "(EventID=42 or EventID=107 or EventID=506 or EventID=507)) or "
              "(Provider[@Name='Microsoft-Windows-Power-Troubleshooter'] and EventID=1)]{since}]")


def _win_time(s: Optional[str]) -> Optional[float]:
    """'2026-09-30T07:11:58.9000000Z' (7 fractional digits, which fromisoformat on 3.9 rejects)."""
    if not s:
        return None
    m = re.match(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?Z?$", s.strip())
    if not m:
        return None
    try:
        base = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None
    return base + (float("0." + m.group(2)) if m.group(2) else 0.0)


def parse_wevtutil_xml(text: str) -> Tuple[List[Dict], List[Record]]:
    """`wevtutil qe ... /f:xml` output (a run of <Event> elements, no root) ->
    (Power-Troubleshooter intervals, Kernel-Power records)."""
    direct, recs = [], []
    body = re.sub(r"<\?xml[^>]*\?>", "", text)
    try:
        root = ET.fromstring("<Events>" + body + "</Events>")
    except ET.ParseError:
        return direct, recs
    for ev in root.iter(_EVT_NS + "Event"):
        sysn = ev.find(_EVT_NS + "System")
        if sysn is None:
            continue
        prov = sysn.find(_EVT_NS + "Provider")
        name = prov.get("Name") if prov is not None else ""
        eid_n = sysn.find(_EVT_NS + "EventID")
        tc = sysn.find(_EVT_NS + "TimeCreated")
        try:
            eid = int((eid_n.text or "").strip()) if eid_n is not None else -1
        except ValueError:
            continue
        t = _win_time(tc.get("SystemTime") if tc is not None else None)
        data = {d.get("Name"): (d.text or "") for d in ev.iter(_EVT_NS + "Data")}
        if name == "Microsoft-Windows-Power-Troubleshooter" and eid == 1:
            s, w = _win_time(data.get("SleepTime")), _win_time(data.get("WakeTime"))
            if s is not None and w is not None and w > s:
                direct.append({"start": s, "end": w, "reason": "sleep (Power-Troubleshooter 1)"})
        elif name == "Microsoft-Windows-Kernel-Power" and t is not None:
            if eid == 42:
                recs.append((t, "sleep", "sleep (Kernel-Power 42)"))
            elif eid == 107:
                recs.append((t, "wake", ""))
            elif eid == 506:
                recs.append((t, "sleep", "modern standby (Kernel-Power 506)"))
            elif eid == 507:
                recs.append((t, "wake", ""))
    return direct, recs


def _windows(env: Env, since, until, cov: Coverage) -> List[Dict]:
    cov.roots_checked.append("wevtutil System log (Kernel-Power, Power-Troubleshooter)")
    cond = ""
    if since is not None:
        lo = datetime.fromtimestamp(since - 30 * 86400, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        cond = f" and TimeCreated[@SystemTime>='{lo}']"
    text = _run(["wevtutil", "qe", "System", "/q:" + _WIN_QUERY.format(since=cond), "/f:xml"], cov, timeout=120)
    if text is None:
        return []
    direct, recs = parse_wevtutil_xml(text)
    if direct or recs:
        cov.roots_found.append("wevtutil System log")
    paired, open_ = pair(recs)
    if open_:
        cov.note("Kernel-Power log ends in a sleep with no resume event; dropped")
    cov.note(f"Windows: {len(direct)} Power-Troubleshooter intervals, {len(paired)} Kernel-Power pairs")
    if recs or direct:
        first = min([r[0] for r in recs] + [d["start"] for d in direct])
        cov.first_record = first  # type: ignore[attr-defined]
        cov.note("Windows System log reaches back to " + datetime.fromtimestamp(first, tz=timezone.utc).date().isoformat())
    return merge(direct + paired)


# ---------------------------------------------------------------- Linux

_SLEEP_START = "6bbd95ee977941e497c48be27c254128"
_SLEEP_STOP = "8811e6df2a8e40f58a94cea26f8ebf14"


def parse_journal_json(text: str) -> List[Record]:
    recs = []
    for line in text.splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if not isinstance(r, dict):
            continue
        try:
            t = int(r.get("__REALTIME_TIMESTAMP")) / 1e6
        except (TypeError, ValueError):
            continue
        mid, msg = r.get("MESSAGE_ID") or "", r.get("MESSAGE")
        msg = msg if isinstance(msg, str) else ""
        if mid == _SLEEP_START or msg.startswith(("Entering sleep state", "Suspending system", "Hibernating system")):
            recs.append((t, "sleep", str(r.get("SLEEP") or "suspend")))
        elif mid == _SLEEP_STOP or msg.startswith(("System returned from sleep", "System resumed")):
            recs.append((t, "wake", ""))
    return recs


def _linux(env: Env, since, until, cov: Coverage) -> List[Dict]:
    cov.roots_checked.append("journalctl _COMM=systemd-sleep")
    cmd = ["journalctl", "-o", "json", "--no-pager", "_COMM=systemd-sleep"]
    if since is not None:
        cmd.insert(3, f"--since=@{int(since - 30 * 86400)}")
    text = _run(cmd, cov, timeout=120)
    if text is None:
        return []
    recs = parse_journal_json(text)
    if recs:
        cov.roots_found.append("journalctl")
        cov.first_record = min(r[0] for r in recs)  # type: ignore[attr-defined]
    out, open_ = pair(recs)
    if open_:
        cov.note("journal ends in a sleep with no resume entry; dropped")
    return out


# ---------------------------------------------------------------- entry point

def sleep_intervals(env: Env, since: Optional[float], until: Optional[float], cov: Coverage) -> List[Dict]:
    try:
        if env.system == "Darwin":
            out, open_ = pair(_macos(env, since, until, cov), mark_log_start=True)
            if open_:
                cov.note("log ends in a sleep with no full Wake yet; dropped")
        elif env.system == "Windows":
            out = _windows(env, since, until, cov)
        elif env.system == "Linux":
            out = _linux(env, since, until, cov)
        else:
            cov.note(f"no sleep-log reader for {env.system}")
            return []
    except Exception as exc:  # a log reader must not sink the study
        cov.note(f"sleep log read failed: {type(exc).__name__}")
        return []
    out = window(out, since, until)
    for iv in out:
        iv["start"], iv["end"] = round(iv["start"], 3), round(iv["end"], 3)
    return out
