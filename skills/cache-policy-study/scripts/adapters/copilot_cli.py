"""Verified 2026-10-08 on a Windows 11 laptop: {HOME}/.copilot/session-state/<uuid>/events.jsonl (92) and
{HOME}/.copilot/session-store.db, 87 sessions; per-session workspace.yaml, session.db and vscode.metadata.json are
not read. The note below is kept for the record.

GitHub Copilot CLI: per-session event logs plus a SQLite session store, under $COPILOT_HOME or ~/.copilot.

Paths ({CH} = $COPILOT_HOME, else {HOME}/.copilot):
  {CH}/session-state/<session id>/events.jsonl    current CLI (1.0.x)
  {CH}/session-state/*.jsonl, *.json              older flat files
  {CH}/history-session-state/*.json[l]            legacy sessions
  {CH}/*.db, *.sqlite, *.sqlite3                  the /chronicle session store (session-store.db in 1.0.73)
  {CH}/logs/                                      process logs (not read; see below)

Formats and mapping. Source of truth: the CLI's own code as bundled in VS Code 1.140
(extensions/copilot/node_modules/@github/copilot 1.0.73: sdk/index.js and the runtime.node schema strings),
read 2026-10-07.
- events.jsonl: one event per line, {type, data, id, timestamp (ISO), parentId, ephemeral?, agentId?}.
  user.message -> prompt when data.source is absent (the CLI's own test for "a person typed this"), else
  auto_prompt (system / sub-agent / autopilot continuation). assistant.message -> response (deduplicated on
  data.messageId), usage null. session.compaction_complete with data.success -> compaction {pre:
  preCompactionTokens, post: postCompactionTokens, trigger: null (not recorded in the event)}. Events with an
  agentId belong to a subagent -> subagent = True. session.start / session.resume data.context.cwd -> project.
  Per-call token counts (assistant.usage) are emitted as EPHEMERAL events, which the CLI never persists to
  events.jsonl; they are only in the session store.
- session store (SQLite, opened read-only; immutable=1 unless a -wal file shows the CLI has it open):
  assistant_usage_events(session_id, agent_id, model, input_tokens, output_tokens, cache_read_tokens,
  cache_write_tokens, initiator, created_at) -> one response per row, with usage. The counts come from an
  OpenAI-style API (prompt_tokens with prompt_tokens_details.cached_tokens / cache_creation_tokens), so
  input_tokens is taken to INCLUDE the cache parts: input = input_tokens - cache_read - cache_write (floored at
  0), ctx = input_tokens + output_tokens. agent_id set -> subagent. turns(session_id, turn_index, timestamp,
  user_message) -> prompt at timestamp when user_message is non-null (only `IS NOT NULL` is selected, never
  the text); used only for sessions with no events.jsonl prompts. sessions(id, cwd) -> project.
  When a session has store usage rows, its events.jsonl assistant.message responses are dropped, so each
  model call is counted once.
- Legacy JSON documents: a list under messages/turns/events/history/exchanges/entries/timeline/chatMessages;
  items with role/type user|human -> prompt and assistant|copilot|model|response -> response, each at the
  item's own timestamp; a document with no per-item times but a start time -> session_start (fidelity session).
- interactive is null: neither the event log nor the store records whether the CLI ran with -p.

Verified 2026-10-07 on this Mac: only {HOME}/.copilot/config.json and 5 logs/process-*.log exist. The logs are
server-mode (stdio) lifecycle lines - start, ready, shutdown, "Destroying 0 active sessions" - with no
per-turn timestamps, so nothing is emitted from them. No session-state, history, or session store exists here,
so every format above is checked against the CLI's code and synthetic fixtures only: VERIFIED = True.

Known gaps: interactive vs -p is unknown; the turns.timestamp is assumed to be the user-message time (the row
is inserted with the user message and the conflict update leaves timestamp alone); compaction trigger is not
recorded; unknown SQLite schemas are skipped, not guessed at.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Set

from cps_common import Coverage, Env, event, in_window, iter_jsonl, parse_time, project_id, usage

TOOL = "copilot-cli"
VERIFIED = False

DOC_LIST_KEYS = ("messages", "turns", "events", "history", "exchanges", "entries", "timeline", "chatMessages")
USER_ROLES = {"user", "human", "user.message"}
ASSISTANT_ROLES = {"assistant", "copilot", "model", "response", "assistant.message"}
SQLITE_GLOBS = ("*.db", "*.sqlite", "*.sqlite3")


def home(env: Env) -> Path:
    base = env.environ.get("COPILOT_HOME")
    return Path(base) if base else env.home / ".copilot"


# ------------------------------------------------------------------ SQLite session store
def _connect_ro(path: Path):
    wal = path.with_name(path.name + "-wal")
    mode = "mode=ro" if wal.exists() else "mode=ro&immutable=1"
    return sqlite3.connect(f"{path.resolve().as_uri()}?{mode}", uri=True, timeout=1.0)


def _usage_from_counts(i, o, cr, cw) -> Optional[Dict]:
    if not any((i, o, cr, cw)):
        return None
    i, o, cr, cw = i or 0, o or 0, cr or 0, cw or 0
    return usage(max(0, i - cr - cw), o, cr, cw, None, None, i + o)


def read_store(path: Path, cov: Coverage) -> Optional[Dict]:
    """{'responses': [...], 'prompts': [...], 'cwd': {sid: cwd}} from a recognised store, else None."""
    try:
        con = _connect_ro(path)
    except sqlite3.Error:
        cov.skip("sqlite unreadable")
        return None
    out = {"responses": [], "prompts": [], "cwd": {}}
    try:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not tables & {"assistant_usage_events", "turns"}:
            cov.skip("sqlite without a recognised session schema")
            return None
        if "sessions" in tables:
            for sid, cwd in con.execute("SELECT id, cwd FROM sessions"):
                out["cwd"][sid] = cwd
        if "assistant_usage_events" in tables:
            q = ("SELECT session_id, agent_id, model, input_tokens, output_tokens, cache_read_tokens, "
                 "cache_write_tokens, created_at FROM assistant_usage_events ORDER BY id")
            for sid, agent, model, i, o, cr, cw, created in con.execute(q):
                t = parse_time(created)
                if sid and t is not None:
                    out["responses"].append((sid, t, model, bool(agent), _usage_from_counts(i, o, cr, cw)))
        if "turns" in tables:
            q = "SELECT session_id, timestamp FROM turns WHERE user_message IS NOT NULL ORDER BY session_id, turn_index"
            for sid, ts in con.execute(q):
                t = parse_time(ts)
                if sid and t is not None:
                    out["prompts"].append((sid, t))
    except sqlite3.Error:
        cov.parse_errors += 1
        return None
    finally:
        con.close()
    return out


# ------------------------------------------------------------------ files
def _is_event_log(rows: List[Dict]) -> bool:
    return any(isinstance(r.get("type"), str) and "." in r["type"] and "data" in r for r in rows[:20])


def read_event_log(rows: List[Dict], sid: str) -> Dict:
    """Events of one events.jsonl -> {'sid', 'cwd', 'prompts', 'responses', 'compactions'}."""
    out = {"sid": sid, "cwd": None, "prompts": [], "responses": [], "compactions": []}
    seen_msg: Set[str] = set()
    model = None
    for r in rows:
        typ, data = r.get("type"), r.get("data") if isinstance(r.get("data"), dict) else {}
        t = parse_time(r.get("timestamp"))
        if typ in ("session.start", "session.resume"):
            out["sid"] = data.get("sessionId") or out["sid"]
            ctx = data.get("context") if isinstance(data.get("context"), dict) else {}
            out["cwd"] = ctx.get("cwd") or out["cwd"]
            model = data.get("selectedModel") or model
        if t is None or r.get("ephemeral"):
            continue
        sub = bool(r.get("agentId"))
        if typ == "user.message" and not sub:
            human = data.get("source") is None and not data.get("isAutopilotContinuation")
            out["prompts"].append((t, "prompt" if human else "auto_prompt"))
        elif typ == "assistant.message":
            mid = data.get("messageId") or r.get("id")
            if mid in seen_msg:
                continue
            seen_msg.add(mid)
            model = data.get("model") or model
            out["responses"].append((t, model, sub))
        elif typ == "session.compaction_complete" and data.get("success") and not sub:
            out["compactions"].append((t, {"pre": data.get("preCompactionTokens"),
                                           "post": data.get("postCompactionTokens"), "trigger": None}))
    return out


def read_document(doc: Dict, sid: str) -> Dict:
    """A legacy whole-session JSON document -> same shape as read_event_log, plus 'start'."""
    out = {"sid": str(doc.get("sessionId") or doc.get("id") or sid),
           "cwd": doc.get("cwd") or doc.get("workingDirectory"),
           "prompts": [], "responses": [], "compactions": [],
           "start": parse_time(doc.get("startTime") or doc.get("createdAt") or doc.get("timestamp"))}
    items = next((doc[k] for k in DOC_LIST_KEYS if isinstance(doc.get(k), list) and doc[k]), [])
    for item in items:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or item.get("type") or "")
        t = parse_time(item.get("timestamp") or item.get("createdAt"))
        if t is None:
            continue
        if role in USER_ROLES:
            out["prompts"].append((t, "prompt"))
        elif role in ASSISTANT_ROLES:
            u = item.get("usage") if isinstance(item.get("usage"), dict) else {}
            out["responses"].append((t, item.get("model") or doc.get("model"), False,
                                     _usage_from_counts(u.get("input_tokens") or u.get("prompt_tokens"),
                                                        u.get("output_tokens") or u.get("completion_tokens"),
                                                        None, None)))
    return out


def _session_files(h: Path) -> List[Path]:
    files = []
    state, legacy = h / "session-state", h / "history-session-state"
    if state.is_dir():
        files += sorted(state.glob("*/events.jsonl")) + sorted(state.glob("*.jsonl")) + sorted(state.glob("*.json"))
    if legacy.is_dir():
        files += sorted(legacy.glob("*.jsonl")) + sorted(legacy.glob("*.json"))
    return files


def _read_file(path: Path, cov: Coverage) -> Optional[Dict]:
    sid = path.parent.name if path.name == "events.jsonl" else path.stem
    if path.suffix == ".jsonl":
        rows = list(iter_jsonl(path, cov))
        if _is_event_log(rows):
            return read_event_log(rows, sid)
        return read_document(rows[0], sid) if len(rows) == 1 else None
    try:
        doc = json.loads(path.read_text(encoding="utf-8-sig", errors="replace"))
    except (OSError, ValueError):
        cov.parse_errors += 1
        return None
    return read_document(doc, sid) if isinstance(doc, dict) else None


# ------------------------------------------------------------------ collect
def collect(env: Env, since, until, cov: Coverage) -> Iterator[Dict]:
    h = home(env)
    for sub in ("session-state", "history-session-state"):
        cov.roots_checked.append(env.placeholder(h / sub))
        if (h / sub).is_dir():
            cov.roots_found.append(env.placeholder(h / sub))
    cov.roots_checked.append(env.placeholder(h) + "/*.db")

    store = {"responses": [], "prompts": [], "cwd": {}}
    if h.is_dir():
        for pattern in SQLITE_GLOBS:
            for path in sorted(h.glob(pattern)):
                cov.files_read += 1
                got = read_store(path, cov)
                if got is None:
                    continue
                cov.roots_found.append(env.placeholder(path))
                for k in ("responses", "prompts"):
                    store[k].extend(got[k])
                store["cwd"].update(got["cwd"])
        logs = sorted((h / "logs").glob("*.log")) if (h / "logs").is_dir() else []
        if logs:
            cov.note(f"{len(logs)} process logs not read: on CLI 1.0.x they hold process lifecycle lines only "
                     "(server start/shutdown), no per-turn timestamps (inspected 2026-10-07)")

    store_usage_sessions = {r[0] for r in store["responses"]}
    prompted: Set[str] = set()
    sessions_seen: Set[str] = set()

    def ev(sid, t, kind, **kw):
        return event(TOOL, sid, t, kind, interactive=None, **kw)

    for path in _session_files(h):
        cov.files_read += 1
        s = _read_file(path, cov)
        if s is None:
            cov.skip("not a recognised session file")
            continue
        sid = s["sid"]
        if sid in sessions_seen:
            cov.skip("duplicate session id")
            continue
        sessions_seen.add(sid)
        proj = project_id(s["cwd"] or store["cwd"].get(sid))
        if s["prompts"]:
            prompted.add(sid)
        for t, kind in s["prompts"]:
            if in_window(t, since, until):
                yield ev(sid, t, kind, project=proj)
        if sid not in store_usage_sessions:
            for row in s["responses"]:
                t, model, subagent = row[0], row[1], row[2]
                if in_window(t, since, until):
                    yield ev(sid, t, "response", project=proj, model=model, subagent=subagent,
                             usage=row[3] if len(row) > 3 else None)
        for t, comp in s["compactions"]:
            if in_window(t, since, until):
                yield ev(sid, t, "compaction", project=proj, compaction=comp)
        if not (s["prompts"] or s["responses"]) and s.get("start") is not None and in_window(s["start"], since, until):
            yield event(TOOL, sid, s["start"], "session_start", interactive=None, fidelity="session", project=proj)

    for sid, t in store["prompts"]:
        if sid not in prompted and in_window(t, since, until):
            yield ev(sid, t, "prompt", project=project_id(store["cwd"].get(sid)))
    for sid, t, model, subagent, u in store["responses"]:
        if in_window(t, since, until):
            yield ev(sid, t, "response", project=project_id(store["cwd"].get(sid)), model=model,
                     subagent=subagent, usage=u)
