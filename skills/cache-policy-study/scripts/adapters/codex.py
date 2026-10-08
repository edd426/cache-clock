"""OpenAI Codex CLI / desktop: one rollout JSONL per thread ($CODEX_HOME wins over ~/.codex).

Paths: {CODEX_HOME}/sessions/YYYY/MM/DD/rollout-*.jsonl and {CODEX_HOME}/archived_sessions/**/rollout-*.jsonl.
Every line is {timestamp, type, payload}; the first is `session_meta` (payload.id, cwd, originator, source).

Field mapping (verified 2026-10-07 on 531 local rollouts, CLI 0.144-0.159, macOS):
- interactive: False when originator == "codex_exec" or source is "exec"/"mcp" (scripted runs); True for any
  other known originator (seen: codex-tui with source cli/vscode, codex_work_desktop with source vscode); None
  when the meta has neither. Rule: originator names the front end that started the thread, and only `codex exec`
  (and the MCP server mode) run without a person at the keyboard.
- subagent threads (source == {"subagent": ...}: thread_spawn children and the approvals "guardian") are separate
  rollouts with their own cache prefix and machine-written prompts. They are skipped, like Claude Code subagent
  transcripts, and counted in a note.
- prompt: event_msg item_completed with item.type == "UserMessage" (one per typed message; 2-3 in one turn are
  mid-turn steering, still human). Older CLIs wrote event_msg user_message instead; used only when a file has
  no UserMessage items. No main-thread turn in the local store lacked a UserMessage, so no auto_prompt is emitted.
- response: `token_usage_record` lines (CLI >= 0.153), one per API response, keyed by response_id. Older files
  only have event_msg token_count, which repeats: a repeat carries the same total_token_usage as the line before
  (100 of 11,790 locally), and a zeroed last_token_usage follows every compaction. Both are dropped; the
  deduplicated token_count stream matched token_usage_record call-for-call in 218 of 220 files that have both
  (the record had one extra call in the other two), so the record wins when present.
  usage: OpenAI's input_tokens INCLUDES the cached part, so input = input_tokens - cached_input_tokens,
  cache_read = cached_input_tokens, cache_write = cache_write_input_tokens (always 0 locally), output =
  output_tokens (includes reasoning), ctx = input_tokens + output_tokens. Timed at the line's timestamp, which is
  written when the response completes (Codex has no request-start stamp per call).
- compaction: the `compacted` line (29 locally; the ContextCompaction item duplicates it and is ignored).
  pre = ctx of the last response before it in the same file (null when none); post = null (the next call's input
  also carries new content, so it would be a guess). trigger = "auto" when it lands mid-turn after a response in
  that turn (the context-limit pattern); null at a turn start, where /compact and pre-turn auto-compaction look
  the same. The desktop app starts a new rollout with a `compacted` line when a long thread rolls over.
- model: the latest turn_context.model (or thread_settings_applied model) before the event.

Known gaps: forked rollouts (forked_from_id) carry an extra copy of the parent's session_meta but do not replay
its turns (checked: no line before the fork's own start, no response_id shared across files). The pre-2025
rollout format without session_meta is not read (none locally); such files are skipped with a reason.
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, Iterator, List, Optional

from cps_common import Coverage, Env, event, in_window, iter_jsonl, parse_time, project_id, usage

TOOL = "codex"
VERIFIED = True

HEADLESS_ORIGINATORS = {"codex_exec"}
HEADLESS_SOURCES = {"exec", "mcp"}


def home(env: Env) -> Path:
    base = env.environ.get("CODEX_HOME")
    return Path(base) if base else env.home / ".codex"


def roots(env: Env) -> List[Path]:
    h = home(env)
    return [h / "sessions", h / "archived_sessions"]


def _interactive(meta: Dict) -> Optional[bool]:
    origin, source = meta.get("originator"), meta.get("source")
    if origin in HEADLESS_ORIGINATORS or (isinstance(source, str) and source in HEADLESS_SOURCES):
        return False
    return True if origin else None


def _is_subagent(meta: Dict) -> bool:
    source = meta.get("source")
    return isinstance(source, dict) and "subagent" in source


def _usage(u: Dict) -> Optional[Dict]:
    i, cached = u.get("input_tokens"), u.get("cached_input_tokens")
    o, cw = u.get("output_tokens"), u.get("cache_write_input_tokens")
    if not any((i, cached, o, cw)):
        return None
    i, cached, o = i or 0, cached or 0, o or 0
    return usage(max(0, i - cached), o, cached, cw, None, None, i + o)


def _read_file(path: Path, cov: Coverage):
    """One rollout -> (session id, meta, list of (t, kind, extra) in file order) or None."""
    meta = None
    items = []                  # (t, kind, model, payload-derived extra)
    prompts_item, prompts_legacy = [], []
    records, counts = [], []    # token_usage_record vs token_count responses
    seen_rid = set()
    prev_total = None
    model = None
    turn_responses = 0          # responses since the last task_started
    last_ctx = None
    for r in iter_jsonl(path, cov):
        typ, p = r.get("type"), r.get("payload")
        if not isinstance(p, dict):
            continue
        if typ == "session_meta":
            if meta is None:
                meta = p
            continue
        t = parse_time(r.get("timestamp"))
        if t is None:
            continue
        ptype = p.get("type")
        if typ == "turn_context":
            model = p.get("model") or model
        elif typ == "event_msg" and ptype == "thread_settings_applied":
            model = (p.get("thread_settings") or {}).get("model") or model
        elif typ == "event_msg" and ptype == "task_started":
            turn_responses = 0
        elif typ == "event_msg" and ptype == "item_completed":
            if (p.get("item") or {}).get("type") == "UserMessage":
                prompts_item.append((t, "prompt", None, None))
        elif typ == "event_msg" and ptype == "user_message":
            prompts_legacy.append((t, "prompt", None, None))
        elif typ == "token_usage_record":
            rid = p.get("response_id")
            u = _usage(p.get("usage") or {})
            if u is None or (rid and rid in seen_rid):
                continue
            seen_rid.add(rid)
            records.append((t, "response", model, u))
            turn_responses += 1
            last_ctx = u["ctx"]
        elif typ == "event_msg" and ptype == "token_count":
            info = p.get("info") or {}
            total = info.get("total_token_usage")
            key = tuple(sorted(total.items())) if isinstance(total, dict) else None
            repeat = key is not None and key == prev_total
            prev_total = key if key is not None else prev_total
            u = _usage(info.get("last_token_usage") or {})
            if u is None or repeat:
                continue
            counts.append((t, "response", model, u))
            if not records:
                turn_responses += 1
                last_ctx = u["ctx"]
        elif typ == "compacted":
            items.append((t, "compaction", model,
                          {"pre": last_ctx, "post": None, "trigger": "auto" if turn_responses else None}))
    if meta is None:
        return None
    items.extend(prompts_item or prompts_legacy)
    items.extend(records or counts)
    items.sort(key=lambda x: x[0])
    return meta, items


def collect(env: Env, since, until, cov: Coverage) -> Iterator[Dict]:
    seen_sessions = set()
    n_sub = 0
    for root in roots(env):
        cov.roots_checked.append(env.placeholder(root))
        if not root.is_dir():
            continue
        cov.roots_found.append(env.placeholder(root))
        for path in sorted(root.rglob("rollout-*.jsonl")):
            cov.files_read += 1
            got = _read_file(path, cov)
            if got is None:
                cov.skip("no session_meta (legacy rollout format)")
                continue
            meta, items = got
            sid = meta.get("id") or meta.get("session_id") or path.stem
            if _is_subagent(meta):
                n_sub += 1
                continue
            if sid in seen_sessions:
                cov.skip("duplicate session id (sessions and archived_sessions)")
                continue
            seen_sessions.add(sid)
            if not items:
                cov.skip("thread with no turns")
                continue
            interactive = _interactive(meta)
            proj = project_id(meta.get("cwd"))
            for t, kind, model, extra in items:
                if not in_window(t, since, until):
                    continue
                if kind == "response":
                    yield event(TOOL, sid, t, kind, interactive=interactive, project=proj, model=model, usage=extra)
                elif kind == "compaction":
                    yield event(TOOL, sid, t, kind, interactive=interactive, project=proj, model=model,
                                compaction=extra)
                else:
                    yield event(TOOL, sid, t, kind, interactive=interactive, project=proj)
    if n_sub:
        cov.note(f"{n_sub} subagent rollouts skipped (thread_spawn children and approval guardians; "
                 "own cache prefixes, machine-written prompts)")
