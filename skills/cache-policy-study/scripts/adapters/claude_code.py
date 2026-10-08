"""Claude Code: one JSONL file per session under ~/.claude/projects/<project>/ ($CLAUDE_CONFIG_DIR wins).

Verified 2026-10-07 on CLI 2.1.292 (macOS). One assistant row per content block, the same usage repeated:
responses are deduplicated on message.id, timed at their first row (closest to the request start, which is
when the cache entry is read or written). `turn_duration` system rows become `turn_end`. Compaction-summary
rows are not prompts; cross-session and teammate messages (stored as meta rows) are `auto_prompt`; typed slash
commands are `prompt`; when a row has `promptSource`, `system` means `auto_prompt` and typed/queued/
suggestion_accepted mean `prompt` (verified 2026-10-07: scheduled-task rows are meta rows with plain text). Subagent transcripts
live in <session>/subagents/: only their responses are read (subagent=True), for the spend ledger — they carry their
own cache prefixes and feed no behaviour. Each subagent response carries `lane` (session/agent) so the TTL
comparison can walk one subagent's requests in order.
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, Iterator, Optional

from cps_common import Coverage, Env, event, in_window, iter_jsonl, parse_time, project_id, usage

TOOL = "claude-code"
VERIFIED = True

# Classified by the row's leading text only; the text itself is never kept.
# Typed by the person (presence): slash commands and `!` shell input.
HUMAN_PREFIXES = ("<command-name", "<command-message", "<bash-input")
# Started by something other than the person: agent/peer messages, notifications, scheduled wake-ups.
AUTO_PREFIXES = ("<task-notification", "<scheduled", "<system-reminder", "<user-memory-input", "<teammate-message",
                 "<agent-message", "<cross-session-message", "Another Claude session", "[Cross-session",
                 "[SYSTEM NOTIFICATION")
# Output echoed back into the transcript, not a new turn.
ECHO_PREFIXES = ("<local-command", "<bash-stdout", "<bash-stderr", "Caveat: The messages below",
                 "[Request interrupted", "[Image")


def roots(env: Env):
    base = env.environ.get("CLAUDE_CONFIG_DIR")
    return [Path(base) / "projects" if base else env.home / ".claude" / "projects"]


def _text_kind(text: str) -> Optional[str]:
    s = text.lstrip()
    if s.startswith(ECHO_PREFIXES):
        return None
    if s.startswith(AUTO_PREFIXES):
        return "auto_prompt"
    return "prompt"


def _prompt_kind(content, is_meta: bool = False) -> Optional[str]:
    """prompt / auto_prompt / None (not a turn start). Meta rows count only when they carry an
    automatic trigger (cross-session messages are stored as meta rows); other meta rows are expansions."""
    if isinstance(content, list):
        types = {b.get("type") for b in content if isinstance(b, dict)}
        if "tool_result" in types:
            return None
        texts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        if not texts:
            return None if is_meta or not types & {"image", "document"} else "prompt"
        kinds = {_text_kind(t) for t in texts} - {None}
        kind = "prompt" if "prompt" in kinds else ("auto_prompt" if kinds else None)
    elif isinstance(content, str):
        kind = _text_kind(content)
    else:
        return None
    if is_meta and kind != "auto_prompt":
        return None
    return kind


# `promptSource` (recent CLI versions) says who started the row; the text prefixes are the fallback.
PERSON_SOURCES = {"typed", "queued", "suggestion_accepted", "sdk"}


def _row_kind(r) -> Optional[str]:
    content = (r.get("message") or {}).get("content")
    source = r.get("promptSource")
    if source == "system":   # scheduled tasks, notifications, peer messages — meta rows included
        return "auto_prompt" if _prompt_kind(content, False) is not None else None
    if source in PERSON_SOURCES:
        return _prompt_kind(content, False)
    return _prompt_kind(content, bool(r.get("isMeta")))


def _response(sid: str, t: float, msg: Dict, interactive: Optional[bool], proj: Optional[str], sub: bool,
              lane: Optional[str] = None) -> Dict:
    u = msg["usage"]
    cc = u.get("cache_creation") or {}
    i, o = u.get("input_tokens") or 0, u.get("output_tokens") or 0
    cr, cw = u.get("cache_read_input_tokens") or 0, u.get("cache_creation_input_tokens") or 0
    e = event(TOOL, sid, t, "response", interactive=interactive, project=proj, model=msg.get("model"),
              subagent=sub, usage=usage(i, o, cr, cw, cc.get("ephemeral_5m_input_tokens"),
                                        cc.get("ephemeral_1h_input_tokens"), i + cr + cw + o))
    if lane:
        e["lane"] = lane     # a subagent's own request sequence (its own cache prefix); the TTL comparison walks it
    return e


def collect(env: Env, since, until, cov: Coverage) -> Iterator[Dict]:
    for root in roots(env):
        cov.roots_checked.append(env.placeholder(root))
        if not root.is_dir():
            continue
        cov.roots_found.append(env.placeholder(root))
        seen = set()   # (session, message id) across resumed copies of a session
        for path in sorted(root.glob("*/*.jsonl")):
            cov.files_read += 1
            entry = None
            for r in iter_jsonl(path, cov):
                if r.get("entrypoint") and entry is None:
                    entry = r["entrypoint"]
                sid = r.get("sessionId")
                t = parse_time(r.get("timestamp"))
                if not sid or t is None or not in_window(t, since, until):
                    continue
                interactive = None if entry is None else not str(entry).startswith("sdk")
                sub = bool(r.get("isSidechain") or r.get("agentId"))
                proj = project_id(r.get("cwd"))
                typ = r.get("type")
                if typ == "assistant":
                    msg = r.get("message") or {}
                    u, mid = msg.get("usage"), msg.get("id")
                    if not u or not mid or msg.get("model") == "<synthetic>" or (sid, mid) in seen:
                        continue
                    seen.add((sid, mid))
                    yield _response(sid, t, msg, interactive, proj, sub,
                                    f"{sid}/{r.get('agentId') or 'sidechain'}" if sub else None)
                elif typ == "user" and not sub and not r.get("isCompactSummary"):
                    kind = _row_kind(r)
                    if kind:
                        yield event(TOOL, sid, t, kind, interactive=interactive, project=proj)
                elif typ == "system" and r.get("subtype") == "turn_duration" and not sub:
                    yield event(TOOL, sid, t, "turn_end", interactive=interactive, project=proj)
                elif typ == "system" and r.get("subtype") == "compact_boundary" and not sub:
                    m = r.get("compactMetadata") or {}
                    yield event(TOOL, sid, t, "compaction", interactive=interactive, project=proj,
                                compaction={"pre": m.get("preTokens"), "post": m.get("postTokens"), "trigger": m.get("trigger")})
        n_sub = 0
        for path in sorted(root.glob("*/*/subagents/*.jsonl")):
            n_sub += 1
            entry = None
            for r in iter_jsonl(path, cov):
                if r.get("entrypoint") and entry is None:
                    entry = r["entrypoint"]
                if r.get("type") != "assistant":
                    continue
                sid, t = r.get("sessionId"), parse_time(r.get("timestamp"))
                msg = r.get("message") or {}
                u, mid = msg.get("usage"), msg.get("id")
                if (not sid or t is None or not in_window(t, since, until) or not u or not mid
                        or msg.get("model") == "<synthetic>" or (sid, mid) in seen):
                    continue
                seen.add((sid, mid))
                yield _response(sid, t, msg, None if entry is None else not str(entry).startswith("sdk"),
                                project_id(r.get("cwd")), True, f"{sid}/{path.stem}")
        if n_sub:
            cov.note(f"{n_sub} subagent transcripts read for spend only (own cache prefixes; not part of the main timeline)")
