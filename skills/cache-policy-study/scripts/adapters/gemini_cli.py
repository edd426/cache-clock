"""Gemini CLI: chat recordings under {GEMINI_CLI_HOME or HOME}/.gemini/tmp/<project>/chats/.

UNVERIFIED (no Gemini CLI store on the machine this was written on). Format read from the v0.63.0 source
(latest stable, 2026-10-06):
  https://github.com/google-gemini/gemini-cli/blob/v0.63.0/packages/core/src/services/chatRecordingTypes.ts
  https://github.com/google-gemini/gemini-cli/blob/v0.63.0/packages/core/src/services/chatRecordingService.ts
  https://github.com/google-gemini/gemini-cli/blob/v0.63.0/packages/core/src/config/storage.ts
  https://github.com/google-gemini/gemini-cli/blob/v0.63.0/packages/core/src/config/projectRegistry.ts
  https://github.com/google-gemini/gemini-cli/blob/v0.63.0/packages/core/src/utils/paths.ts
  https://github.com/google-gemini/gemini-cli/blob/v0.63.0/packages/core/src/core/logger.ts
  https://github.com/google-gemini/gemini-cli/blob/v0.63.0/packages/core/src/agents/local-executor.ts

Layout: home = $GEMINI_CLI_HOME or the user's home; runtime dir <home>/.gemini (macOS sandbox-exec:
<home>/.cache/.gemini). Per project tmp/<slug>/ (since v0.29; before that tmp/<sha256(project root)>/, migrated
on startup) holding .project_root (the project path), logs.json and chats/:
  - chats/session-<YYYY-MM-DDTHH-MM>-<id8>.jsonl  (since v0.39): line 1 = metadata {sessionId, projectHash,
    startTime, lastUpdated, kind}; then message lines {id, timestamp, type, content, tokens, model, ...} where a
    message id can repeat (last copy wins), {"$set": {...}} (a `messages` array inside replaces the history),
    {"$rewindTo": id} (drop that message and all after), {"$patch": {updates, removeIds, orderIds}} (v0.64+).
  - chats/session-*.json (before v0.39): one object {sessionId, projectHash, startTime, lastUpdated, messages}.
  - chats/<parentSessionId>/<agentId>.jsonl: subagent recordings (kind "subagent") — skipped and counted.
  - checkpoint-*.json, checkpoints/, logs/, shell_history: not read.
  - logs.json: [{sessionId, messageId, timestamp, type: "user", message}] — prompts only; used only for sessions
    with no chat recording.

Mapping: type "user" -> prompt; type "gemini" -> response (info/error/warning ignored). tokens (TokensSummary,
from Gemini usageMetadata): input = promptTokenCount, which INCLUDES cached = cachedContentTokenCount, output =
candidatesTokenCount, thoughts = thoughtsTokenCount. So usage.input = input - cached, cache_read = cached,
output = output + thoughts (reasoning included, like the codex adapter), cache_write = None (Gemini's implicit
cache records no writes), ctx = input + output + thoughts. Project = project_id(.project_root contents) so it
matches other tools' hashes of the same path; else project_id(projectHash).
Fidelity: turn. interactive: null — `gemini -p` records through the same path with no marker.

Check first on a real machine: coverage sessions > 0, the per-month histogram spans both the .json era and the
.jsonl era if the user is a long-time user (a gap at 2026-0x would mean one of the two readers sees nothing), and
that kinds has both prompt and response.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Iterator, List, Optional

from cps_common import Coverage, Env, event, in_window, iter_jsonl, parse_time, project_id, usage

TOOL = "gemini-cli"
VERIFIED = False


def roots(env: Env) -> List[Path]:
    base = Path(env.environ["GEMINI_CLI_HOME"]) if env.environ.get("GEMINI_CLI_HOME") else env.home
    return [base / ".gemini" / "tmp", base / ".cache" / ".gemini" / "tmp"]


def _load_json(path: Path, cov: Coverage):
    try:
        with open(path, "rb") as f:
            return json.load(f)
    except OSError:
        cov.skip("unreadable")
    except ValueError:
        cov.parse_errors += 1
    return None


def load_session(path: Path, cov: Coverage) -> Optional[Dict]:
    """Rebuild a session the way the CLI's loadConversationRecord does: {meta..., "messages": [...]}."""
    if path.suffix == ".json":
        obj = _load_json(path, cov)
        return obj if isinstance(obj, dict) else None
    meta: Dict = {}
    order: List[str] = []
    msgs: Dict[str, Dict] = {}
    first = True
    for r in iter_jsonl(path, cov):
        if first and "messages" not in r and "type" not in r:
            meta.update(r)
            first = False
            continue
        first = False
        if "$set" in r and isinstance(r["$set"], dict):
            s = dict(r["$set"])
            if isinstance(s.get("messages"), list):
                order, msgs = [], {}
                for m in s.pop("messages"):
                    if isinstance(m, dict) and m.get("id"):
                        order.append(m["id"])
                        msgs[m["id"]] = m
            meta.update(s)
        elif "$rewindTo" in r:
            rid = r["$rewindTo"]
            if rid in msgs:
                cut = order.index(rid)
                for mid in order[cut:]:
                    msgs.pop(mid, None)
                order = order[:cut]
        elif "$patch" in r and isinstance(r["$patch"], dict):
            p = r["$patch"]
            for mid in p.get("removeIds") or []:
                if mid in msgs:
                    msgs.pop(mid)
                    order.remove(mid)
            ids = p.get("orderIds")
            if isinstance(ids, list) and set(ids) == set(order):
                order = list(ids)
        elif r.get("id") and r.get("type"):
            if r["id"] not in msgs:
                order.append(r["id"])
            msgs[r["id"]] = r
    if not meta and not msgs:
        return None
    meta["messages"] = [msgs[m] for m in order]
    return meta


def _usage(tok) -> Optional[Dict]:
    if not isinstance(tok, dict):
        return None
    i, c, o, th = tok.get("input"), tok.get("cached"), tok.get("output"), tok.get("thoughts")
    if not any((i, c, o, th)):
        return None
    i, c, o, th = i or 0, c or 0, o or 0, th or 0
    return usage(max(0, i - c), o + th, c, None, None, None, i + o + th)


def _project(slug_dir: Path, meta: Dict) -> Optional[str]:
    try:
        root = (slug_dir / ".project_root").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        root = ""
    return project_id(root or meta.get("projectHash"))


def collect(env: Env, since, until, cov: Coverage) -> Iterator[Dict]:
    seen_sessions = set()
    for root in roots(env):
        cov.roots_checked.append(env.placeholder(root))
        if not root.is_dir():
            continue
        cov.roots_found.append(env.placeholder(root))
        cov.note("interactive unknown: `gemini -p` runs are recorded like interactive sessions (no marker)")
        n_sub = 0
        for slug_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            chats = slug_dir / "chats"
            # .jsonl first: a resumed .json is migrated to .jsonl, and the newer copy should win the dedupe
            files = sorted(chats.glob("*.jsonl")) + sorted(chats.glob("*.json")) if chats.is_dir() else []
            n_sub += sum(1 for _ in chats.glob("*/*.jsonl")) if chats.is_dir() else 0
            proj = None
            for path in files:
                cov.files_read += 1
                s = load_session(path, cov)
                if not s:
                    cov.skip("empty-or-malformed")
                    continue
                if s.get("kind") == "subagent":
                    n_sub += 1
                    continue
                sid = s.get("sessionId") or path.stem
                if sid in seen_sessions:   # a .json migrated to .jsonl on resume: read once
                    cov.skip("duplicate-session")
                    continue
                seen_sessions.add(sid)
                proj = proj or _project(slug_dir, s)
                for m in s.get("messages") or []:
                    if not isinstance(m, dict):
                        continue
                    t = parse_time(m.get("timestamp"))
                    if t is None or not in_window(t, since, until):
                        continue
                    if m.get("type") == "user":
                        yield event(TOOL, sid, t, "prompt", project=proj)
                    elif m.get("type") == "gemini":
                        yield event(TOOL, sid, t, "response", project=proj, model=m.get("model"),
                                    usage=_usage(m.get("tokens")))
            # logs.json: prompts of sessions that have no chat recording (pre-recording versions, deleted chats)
            logs = slug_dir / "logs.json"
            if logs.is_file():
                cov.files_read += 1
                rows = _load_json(logs, cov)
                extra = 0
                for r in rows if isinstance(rows, list) else []:
                    if not isinstance(r, dict) or r.get("type") != "user" or r.get("sessionId") in seen_sessions:
                        continue
                    t = parse_time(r.get("timestamp"))
                    if t is None or not r.get("sessionId") or not in_window(t, since, until):
                        continue
                    extra += 1
                    yield event(TOOL, str(r["sessionId"]), t, "prompt",
                                project=proj or _project(slug_dir, {}))
                if extra:
                    cov.note("some sessions known only from logs.json (prompts, no responses)")
        if n_sub:
            cov.note(f"{n_sub} subagent recordings skipped (not part of the main timeline)")
