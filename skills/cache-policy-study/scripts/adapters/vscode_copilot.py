"""VS Code Copilot Chat: one file per chat session, in two formats.

Paths ({APP} = env.app_support(): macOS ~/Library/Application Support, Windows %APPDATA%, Linux ~/.config;
{FLAVOUR} = Code, Code - Insiders, VSCodium):
  {APP}/{FLAVOUR}/User/workspaceStorage/<workspace hash>/chatSessions/*.json[l]
  {APP}/{FLAVOUR}/User/globalStorage/emptyWindowChatSessions/*.json[l]
  {APP}/{FLAVOUR}/User/globalStorage/*/chatSessions/*.json[l]

Formats:
- Older sessions: one JSON object {sessionId, creationDate, lastMessageDate, requests[], ...}.
- Newer sessions (.jsonl, VS Code >= early 2026): a mutation log, NOT one document per line. Line 1 is
  {"kind":0,"v":<state>}; later lines patch it: kind 1 = set value at path "k", kind 2 = push "v" items onto the
  array at "k" after truncating it to length "i" when given, kind 3 = delete at "k". VS Code rewrites the file
  as a single kind-0 line after 512 entries. Requests appended after line 1 live only in kind-2 lines, so a
  reader that parses line 1 (or each line as a session) misses them. Replay rules copied from VS Code 1.140's
  workbench bundle (`read`, `_applySet`, `_applyPush`), checked 2026-10-07.

Field mapping (per request in state.requests):
- prompt at request.timestamp (epoch ms); auto_prompt when request.isSystemInitiated is true.
- response at the completion time: modelState.completedAt when modelState.value is 1 (complete) or 3 (failed);
  for the pre-modelState format, timestamp + result.timings.totalElapsed unless isCanceled. Value 2
  (cancelled) is skipped: VS Code serializes a still-running response as {value:2, completedAt:<save time>}, so
  that time is not a completion. model = request.modelId.
- usage is always null. VS Code 1.140 does serialize promptTokens/completionTokens per request, but one request
  spans every model call of an agent-mode tool loop and the cached share is not split out, so mapping it onto
  one response would be a guess.
- interactive = True (chat panel; there is no scripted mode). project = project_id(<workspace hash folder name>);
  null for global/empty-window sessions.
- Imported sessions (isImported) are skipped: VS Code rewrites their timestamps to import-time + index.

Verified 2026-10-07 on this Mac (VS Code 1.140): store layout and both envelope formats read (17 old JSON, 29
JSONL in workspaceStorage, 1 empty-window JSON), but every one of the 47 sessions has zero requests, so no
request-level event was produced from real data. The request fields and the mutation-log replay are checked
against VS Code's own source and the synthetic fixtures only; hence VERIFIED = False.

Known gaps: chatEditingSessions (edit timelines) are not read; remote (.vscode-server) and Cursor/Antigravity
forks are separate adapters or not covered.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from datetime import datetime, timezone

from cps_common import Coverage, Env, event, in_window, parse_time, project_id

TOOL = "vscode-copilot"
VERIFIED = False

FLAVOURS = ("Code", "Code - Insiders", "VSCodium")
DONE_STATES = (1, 3)   # modelState.value: 0 pending, 1 complete, 2 cancelled, 3 failed, 4 needs input


def roots(env: Env) -> List[Path]:
    base = env.app_support()
    return [base / f / "User" for f in FLAVOURS] if base else []


def _session_files(root: Path) -> List[Tuple[Path, Optional[str]]]:
    """(file, workspace hash or None) for every chat session file under one User folder."""
    out, seen = [], set()
    patterns = [("workspaceStorage/*/chatSessions/*.json*", True),
                ("globalStorage/emptyWindowChatSessions/*.json*", False),
                ("globalStorage/*/chatSessions/*.json*", False)]
    for pattern, in_workspace in patterns:
        for path in sorted(root.glob(pattern)):
            if path in seen or not path.is_file():
                continue
            seen.add(path)
            out.append((path, path.parent.parent.name if in_workspace else None))
    return out


def _walk(state: Any, keys: List[Any]) -> Any:
    for k in keys:
        state = state[k]
    return state


def replay(lines: List[str], cov: Optional[Coverage] = None) -> Optional[Dict]:
    """Rebuild a session from the JSONL mutation log, as VS Code's ObjectMutationLog.read does."""
    state = None
    for raw in lines:
        if not raw.strip():
            continue
        try:
            entry = json.loads(raw)
            kind, path = entry.get("kind"), entry.get("k") or []
            if kind == 0:
                state = entry.get("v")
                continue
            if state is None or not isinstance(path, list):
                raise ValueError("entry before the initial state")
            if kind in (1, 3):
                if not path:
                    continue
                parent = _walk(state, path[:-1])
                if kind == 1:
                    parent[path[-1]] = entry.get("v")
                elif isinstance(parent, dict):
                    parent.pop(path[-1], None)
                else:
                    parent[path[-1]] = None
            elif kind == 2:
                parent = _walk(state, path[:-1])
                arr = parent.get(path[-1]) if isinstance(parent, dict) else parent[path[-1]]
                arr = list(arr or [])
                if isinstance(entry.get("i"), int):
                    del arr[entry["i"]:]
                arr.extend(entry.get("v") or [])
                parent[path[-1]] = arr
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            if cov:
                cov.parse_errors += 1
    return state if isinstance(state, dict) else None


def read_session(path: Path, cov: Optional[Coverage] = None) -> Optional[Dict]:
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        if cov:
            cov.skip("unreadable")
        return None
    if not text.strip():
        return None
    try:
        doc = json.loads(text)
    except ValueError:
        doc = None
    if isinstance(doc, dict):
        if doc.get("kind") == 0 and isinstance(doc.get("v"), dict):   # a log with only its initial line
            return doc["v"]
        return doc
    state = replay(text.splitlines(), cov)
    if state is None and cov:
        cov.parse_errors += 1
    return state


def _response_time(req: Dict) -> Optional[float]:
    ms = req.get("modelState")
    if isinstance(ms, dict):
        return parse_time(ms.get("completedAt")) if ms.get("value") in DONE_STATES else None
    if req.get("isCanceled"):
        return None
    start = parse_time(req.get("timestamp"))
    timings = (req.get("result") or {}).get("timings") if isinstance(req.get("result"), dict) else None
    elapsed = timings.get("totalElapsed") if isinstance(timings, dict) else None
    if start is None or not isinstance(elapsed, (int, float)):
        return None
    return start + elapsed / 1000.0


def collect(env: Env, since, until, cov: Coverage) -> Iterator[Dict]:
    empty = imported = untimed = 0
    formats = {}   # file suffix -> [count, first creation month, last creation month]
    for root in roots(env):
        cov.roots_checked.append(env.placeholder(root))
        if not root.is_dir():
            continue
        cov.roots_found.append(env.placeholder(root))
        seen = set()
        for path, workspace in _session_files(root):
            cov.files_read += 1
            doc = read_session(path, cov)
            if doc is None:
                continue
            created = parse_time(doc.get("creationDate"))
            month = datetime.fromtimestamp(created, tz=timezone.utc).strftime("%Y-%m") if created else "?"
            f = formats.setdefault(path.suffix, [0, month, month])
            f[0], f[1], f[2] = f[0] + 1, min(f[1], month), max(f[2], month)
            sid = str(doc.get("sessionId") or path.stem)
            if sid in seen:
                cov.skip("duplicate session id")
                continue
            seen.add(sid)
            if doc.get("isImported"):
                imported += 1
                cov.skip("imported session (synthetic timestamps)")
                continue
            requests = doc.get("requests") if isinstance(doc.get("requests"), list) else []
            if not requests:
                empty += 1
                cov.skip("session with no requests")
                continue
            proj = project_id(workspace)
            for req in requests:
                if not isinstance(req, dict):
                    continue
                t = parse_time(req.get("timestamp"))
                if t is None:
                    untimed += 1
                    continue
                model = req.get("modelId") or doc.get("modelId")
                if in_window(t, since, until):
                    kind = "auto_prompt" if req.get("isSystemInitiated") else "prompt"
                    yield event(TOOL, sid, t, kind, interactive=True, project=proj, model=model)
                done = _response_time(req)
                if done is not None and done >= t and in_window(done, since, until):
                    yield event(TOOL, sid, done, "response", interactive=True, project=proj, model=model)
    if formats:
        cov.note("sessions read by format: " + ", ".join(
            f"{suffix or '(none)'} {n} (created {lo}..{hi})" for suffix, (n, lo, hi) in sorted(formats.items())))
    if empty:
        cov.note(f"{empty} sessions have no requests (chat view opened, nothing asked); no events emitted for them")
    if imported:
        cov.note(f"{imported} imported sessions skipped (VS Code rewrites their timestamps on import)")
    if untimed:
        cov.note(f"{untimed} requests without a timestamp skipped")
