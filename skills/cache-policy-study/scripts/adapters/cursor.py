"""Verified 2026-10-08 on a Windows 11 laptop: {APPDATA}/Cursor/User/globalStorage/state.vscdb + workspaceStorage/<hash>/,
110 sessions, 147 empty composers, aiService.generations read as one pseudo-session per workspace. The note below
is kept for the record.

Cursor (the IDE): SQLite key/value stores {app_support}/Cursor/User/{globalStorage,workspaceStorage/*}/state.vscdb.

UNVERIFIED (no Cursor install on the machine this was written on). Format from open-source exporters (read
2026-10-07; their version labels, not Cursor release notes):
  https://github.com/specstoryai/getspecstory/blob/HEAD/specstory-cli/pkg/providers/cursoride/CURSORIDE-FORMAT.md
  https://github.com/specstoryai/getspecstory/blob/HEAD/specstory-cli/pkg/providers/cursoride/types.go
  https://github.com/specstoryai/getspecstory/blob/HEAD/specstory-cli/pkg/providers/cursoride/workspace.go
  https://github.com/S2thend/cursor-history/blob/HEAD/src/core/storage.ts   (timestamp fallback order)
  https://github.com/S2thend/cursor-history/blob/HEAD/specs/010-fix-timestamp-fallback/research.md
  https://github.com/kenn-io/agentsview/blob/HEAD/internal/parser/cursor_ide.go
  https://github.com/saharmor/cursor-view/blob/HEAD/server.py
  https://github.com/org2AI/ORG2/blob/HEAD/docs/cursor-ide-metadata.md

Global DB, table cursorDiskKV(key, value=JSON), Cursor 0.43+ (current through 3.x):
  composerData:<composerId>  {createdAt, lastUpdatedAt (epoch ms), modelConfig.modelName,
      workspaceIdentifier.uri.fsPath (3.12+), trackedGitRepos[0].repoPath, conversation[] (inline bubbles, _v<3)}
  bubbleId:<composerId>:<bubbleId>  {type (1 user, 2 assistant), createdAt (ISO, ~2025-09+),
      timingInfo.{clientRpcSendTime, clientSettleTime, clientEndTime} (epoch ms; clientStartTime is NOT epoch),
      tokenCount.{inputTokens, outputTokens} (almost always 0), modelInfo.modelName}
Workspace DBs, table ItemTable: composer.composerData {allComposers: [{composerId, createdAt, lastUpdatedAt}]}
(0.43 .. 2.x; maps composers to the folder in the sibling workspace.json) and aiService.generations
[{unixMs, ...}] (older Cursor; per-request times with no conversation id).

Mapping: bubble type 1 -> prompt, type 2 -> response (fidelity turn), timed at createdAt, else the first epoch-ms
timingInfo field (cursor-history's order). One response per assistant BUBBLE: an agent turn writes a bubble per
tool step, so response counts exceed API calls (gaps between turns are unaffected). usage only when tokenCount is
non-zero (input/output; cache fields unknown). A composer with bubbles but no bubble times -> session_start /
session_end at createdAt / lastUpdatedAt (fidelity session); one with no bubbles and < 1 s between the two is an
empty tab and is skipped. Composers known only from a workspace DB -> session bounds. aiService.generations ->
`activity` (fidelity turn) in one pseudo-session per workspace ("aiService:<hash>"), since it has no conversation
id. project = project_id(local path) from workspaceIdentifier, else the workspace.json folder, else the first
tracked git repo. interactive: null. Fields are pulled with SQLite json_extract (message text is never loaded);
without the JSON1 extension the value is parsed in Python and only the same fields are kept.

Check first on a real machine: sessions > 0 and fidelity mostly "turn" (if mostly "session", bubble timestamps
moved — re-probe a bubble's keys); the per-month histogram has no hole around 2025-09 (the createdAt switch);
notes report how many composers were empty.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple
from urllib.parse import unquote, urlparse

from cps_common import Coverage, Env, event, in_window, parse_time, project_id, usage

from ._sqlite_ro import open_ro

TOOL = "cursor"
VERIFIED = True

BUBBLE_FIELDS = ["type", "createdAt", "timingInfo.clientRpcSendTime", "timingInfo.clientSettleTime",
                 "timingInfo.clientEndTime", "tokenCount.inputTokens", "tokenCount.outputTokens",
                 "modelInfo.modelName"]
COMPOSER_FIELDS = ["createdAt", "lastUpdatedAt", "modelConfig.modelName", "workspaceIdentifier.uri.fsPath",
                   "trackedGitRepos.0.repoPath"]


def roots(env: Env) -> List[Path]:
    base = env.app_support()
    return [base / "Cursor" / "User"] if base else []


# ----------------------------------------------------------------------------- JSON field access

def _jpath(dotted: str) -> str:
    if not dotted:
        return "$"
    return "$" + "".join(f"[{p}]" if p.isdigit() else f".{p}" for p in dotted.split("."))


def _get(obj, dotted: str):
    if not dotted:
        return obj
    for p in dotted.split("."):
        if isinstance(obj, list) and p.isdigit():
            obj = obj[int(p)] if int(p) < len(obj) else None
        elif isinstance(obj, dict):
            obj = obj.get(p)
        else:
            return None
    return obj


def json_rows(con: sqlite3.Connection, table: str, where: str, args: tuple, paths: List[str],
              each: Optional[str] = None) -> List[Tuple]:
    """[(key, *values at paths)] for rows of `table` matching `where`; with `each`, one row per element of the
    array at that path ("" = the value itself is the array). SQL json_extract first; Python json parsing when the JSON1 extension is missing."""
    src = f"(SELECT key, CAST(value AS TEXT) AS v FROM {table} WHERE {where} AND json_valid(CAST(value AS TEXT)))"
    if each is not None:
        cols = ", ".join(f"json_extract(e.value, '{_jpath(p)}')" for p in paths)
        sql = f"SELECT k.key, {cols} FROM {src} AS k, json_each(k.v, '{_jpath(each)}') AS e"
    else:
        cols = ", ".join(f"json_extract(k.v, '{_jpath(p)}')" for p in paths)
        sql = f"SELECT k.key, {cols} FROM {src} AS k"
    try:
        return con.execute(sql, args).fetchall()
    except sqlite3.OperationalError as exc:
        if "json" not in str(exc).lower():
            raise
    out = []
    for key, value in con.execute(f"SELECT key, value FROM {table} WHERE {where}", args):
        try:
            obj = json.loads(value)
        except (TypeError, ValueError):
            continue
        items = _get(obj, each) if each is not None else [obj]
        for it in items if isinstance(items, list) else []:
            out.append((key,) + tuple(_get(it, p) if isinstance(it, dict) else None for p in paths))
    return out


def _ms(v) -> Optional[float]:
    """An epoch-ms number (timingInfo); smaller values are relative clocks, not times."""
    return v / 1000.0 if isinstance(v, (int, float)) and v >= 1e12 else None


def bubble_time(created, *timing) -> Optional[float]:
    t = parse_time(created) if created not in (None, "") else None
    if t is not None and t > 1e9:
        return t
    for v in timing:
        t = _ms(v)
        if t is not None:
            return t
    return None


def folder_path(uri: Optional[str]) -> Optional[str]:
    """workspace.json folder URI -> local path (file:///C:/x -> C:/x); other schemes are kept whole."""
    if not uri:
        return None
    u = urlparse(uri)
    if u.scheme != "file":
        return uri
    p = unquote(u.path)
    return p[1:] if len(p) > 2 and p[0] == "/" and p[2] == ":" else p


# ----------------------------------------------------------------------------- readers

def read_workspaces(user: Path, cov: Coverage):
    """-> ({composerId: (project, createdAt, lastUpdatedAt)}, [(pseudo-session, project, [t])])"""
    comps: Dict[str, Tuple] = {}
    gens: List[Tuple[str, Optional[str], List[float]]] = []
    n_chat = 0
    wsroot = user / "workspaceStorage"
    for d in sorted(wsroot.glob("*")) if wsroot.is_dir() else []:
        db = d / "state.vscdb"
        if not db.is_file():
            continue
        try:
            ws = json.loads((d / "workspace.json").read_text(encoding="utf-8"))
            folder = folder_path(ws.get("folder") or ws.get("workspace"))
        except (OSError, ValueError, AttributeError):
            folder = None
        proj = project_id(folder)
        cov.files_read += 1
        with open_ro(db, cov) as con:
            if con is None:
                continue
            try:
                for _, cid, c0, c1 in json_rows(con, "ItemTable", "key = ?", ("composer.composerData",),
                                                ["composerId", "createdAt", "lastUpdatedAt"], each="allComposers"):
                    if cid:
                        comps[str(cid)] = (proj, c0, c1)
                ts = []
                for row in json_rows(con, "ItemTable", "key = ?", ("aiService.generations",), ["unixMs"], each=""):
                    t = parse_time(row[1])
                    if t is not None:
                        ts.append(t)
                if ts:
                    gens.append(("aiService:" + project_id(d.name), proj, ts))
                n_chat += con.execute("SELECT count(*) FROM ItemTable WHERE key IN (?, ?)",
                                      ("workbench.panel.aichat.view.aichat.chatdata",
                                       "workbench.panel.chat.view.chat.chatdata")).fetchone()[0]
            except sqlite3.Error:
                cov.parse_errors += 1
    if n_chat:
        cov.note(f"{n_chat} workspaces have legacy chat-panel data (not read; sparse timestamps)")
    return comps, gens


def read_global(user: Path, cov: Coverage):
    """-> ({composerId: composer fields dict}, {composerId: [bubble field tuples]})"""
    db = user / "globalStorage" / "state.vscdb"
    composers: Dict[str, Dict] = {}
    bubbles: Dict[str, List[Tuple]] = {}
    if not db.is_file():
        return composers, bubbles
    cov.files_read += 1
    with open_ro(db, cov) as con:
        if con is None:
            return composers, bubbles
        try:
            if not con.execute("SELECT 1 FROM sqlite_master WHERE name = 'cursorDiskKV'").fetchone():
                cov.note("global state.vscdb has no cursorDiskKV table (Cursor older than 0.43?)")
                return composers, bubbles
            for row in json_rows(con, "cursorDiskKV", "key >= ? AND key < ?", ("composerData:", "composerData;"),
                                 COMPOSER_FIELDS):
                composers[row[0][len("composerData:"):]] = dict(zip(COMPOSER_FIELDS, row[1:]))
            for row in json_rows(con, "cursorDiskKV", "key >= ? AND key < ?", ("bubbleId:", "bubbleId;"),
                                 BUBBLE_FIELDS):
                parts = row[0].split(":")
                if len(parts) >= 3:
                    bubbles.setdefault(parts[1], []).append(row[1:])
            for row in json_rows(con, "cursorDiskKV", "key >= ? AND key < ?", ("composerData:", "composerData;"),
                                 BUBBLE_FIELDS, each="conversation"):
                bubbles.setdefault(row[0][len("composerData:"):], []).append(row[1:])
        except sqlite3.Error:
            cov.parse_errors += 1
    return composers, bubbles


# ----------------------------------------------------------------------------- collect

def _bubble_usage(i, o) -> Optional[Dict]:
    i = i if isinstance(i, int) else None
    o = o if isinstance(o, int) else None
    return usage(i, o) if (i or o) else None


def _bounds(cid: str, c0, c1, proj, model, since, until) -> Iterator[Dict]:
    t0, t1 = parse_time(c0), parse_time(c1)
    if t0 is not None and in_window(t0, since, until):
        yield event(TOOL, cid, t0, "session_start", fidelity="session", project=proj, model=model)
    if t1 is not None and t1 != t0 and in_window(t1, since, until):
        yield event(TOOL, cid, t1, "session_end", fidelity="session", project=proj, model=model)


def collect(env: Env, since, until, cov: Coverage) -> Iterator[Dict]:
    for user in roots(env):
        cov.roots_checked.append(env.placeholder(user / "globalStorage" / "state.vscdb"))
        cov.roots_checked.append(env.placeholder(user / "workspaceStorage"))
        if not user.is_dir():
            continue
        cov.roots_found.append(env.placeholder(user))
        ws_comps, gens = read_workspaces(user, cov)
        composers, bubbles = read_global(user, cov)
        n_empty = n_session = 0
        for cid in sorted(set(composers) | set(ws_comps)):
            c = composers.get(cid) or {}
            wproj, w0, w1 = ws_comps.get(cid, (None, None, None))
            local = c.get("workspaceIdentifier.uri.fsPath") or None
            proj = project_id(local) if local else (wproj or project_id(c.get("trackedGitRepos.0.repoPath")))
            model = c.get("modelConfig.modelName")
            bs = bubbles.get(cid, [])
            timed = []
            for typ, created, rpc, settle, end, ti, to, bmodel in bs:
                t = bubble_time(created, rpc, settle, end)
                if t is not None and typ in (1, 2):
                    timed.append((t, typ, ti, to, bmodel))
            if timed:
                for t, typ, ti, to, bmodel in sorted(timed):
                    if not in_window(t, since, until):
                        continue
                    if typ == 1:
                        yield event(TOOL, cid, t, "prompt", project=proj)
                    else:
                        yield event(TOOL, cid, t, "response", project=proj, model=bmodel or model,
                                    usage=_bubble_usage(ti, to))
                continue
            c0, c1 = c.get("createdAt") or w0, c.get("lastUpdatedAt") or w1
            t0, t1 = parse_time(c0), parse_time(c1)
            if not bs and (t0 is None or t1 is None or t1 - t0 < 1):
                n_empty += 1
                continue
            n_session += 1
            yield from _bounds(cid, c0, c1, proj, model, since, until)
        for sid, proj, ts in gens:
            for t in sorted(ts):
                if in_window(t, since, until):
                    yield event(TOOL, sid, t, "activity", project=proj)
        if n_empty:
            cov.note(f"{n_empty} empty composers skipped (no bubbles, no duration)")
        if n_session:
            cov.note(f"{n_session} composers had no bubble timestamps (session bounds only)")
        if gens:
            cov.note("aiService.generations read as activity, one pseudo-session per workspace")
