"""Checked 2026-10-08 on a Windows 11 laptop — PARTIAL. Only {HOME}/.gemini/antigravity-cli exists there
(conversations/<uuid>.db 71, brain/<uuid>/.system_generated/logs/transcript.jsonl 64). The .db tables are as
expected and steps.metadata field 1 is a Timestamp (prompts decode), but gen_metadata.data -> chatModel carries
no timestamp anywhere (field 9 = {2: uint64 id, 10: {1, ...}}), so all 11,667 model calls are dropped (t = None);
usage sits at chatModel.4 in 11,616 rows. Responses therefore come from transcript.jsonl (times, no usage): 0
tokens reach the spend ledger. Fix: join gen_metadata to steps (or the transcript) for a time. Not fixed yet.

Google Antigravity (agentic IDE and its `agy` CLI): conversations under {HOME}/.gemini/antigravity*/.

UNVERIFIED (no Antigravity install on the machine this was written on); nothing here comes from official docs,
which document no storage paths. Sources (read 2026-10-07):
  https://github.com/getagentseal/codeburn  src/providers/antigravity.ts (roots, .db SQL, protobuf field map),
      tests/fixtures/antigravity-cli-current/.../transcript.jsonl (transcript row shape)
  https://github.com/FutureisinPast/antigravity-conversation-fix  rebuild_conversations.py
  https://github.com/michaelw9999/antigravity-cli  (brain/*.metadata.json fields)
  https://pi.dev/packages/@estebanforge/pi-ask-antigravity  (AGY_CONVERSATIONS_DIR)
  https://discuss.ai.google.dev/t/data-loss-encrypted-chats-permanently-bricked-after-pc-crash-files-on-disk-but-unrecoverable/135502

Roots (Windows: %USERPROFILE%\\.gemini\\...): .gemini/antigravity (IDE v1), .gemini/antigravity-ide (IDE v2),
.gemini/antigravity-cli (agy CLI; conversations dir movable via $AGY_CONVERSATIONS_DIR), .gemini/antigravity-backup
(upgrade leftovers; a conversation id already seen elsewhere is skipped). Each may hold:
  conversations/<uuid>.db  SQLite (v2 IDE and CLI). Protobuf blobs, field map from codeburn:
      gen_metadata(idx, data): data.1 = chatModel; chatModel.4 = usage {2 uncached input, 3 output incl.
      thinking, 4 cache write, 5 cache read}; chatModel.9.4 = created_at (google.protobuf.Timestamp {1 s, 2 ns});
      chatModel.19 = model id (21 = display name). One row per model call -> `response` with usage.
      steps(idx, step_type, metadata): metadata.1 = Timestamp; step_type 14 = user input -> `prompt`.
  conversations/<uuid>.pb  ENCRYPTED (key tied to the running app; readable only through the app's local RPC) —
      no timestamps inside are readable, so only the file mtime is used.
  brain/<uuid>/.system_generated/logs/transcript.jsonl  one JSON row per step: {step_index, source, type, status,
      created_at (ISO), ...}. type USER_INPUT + source USER_EXPLICIT -> prompt; other USER_INPUT -> auto_prompt;
      type PLANNER_RESPONSE (source MODEL) -> response (no usage in this file).
  brain/<uuid>/*.metadata.json  {artifactType, updatedAt (ISO), version, ...} -> activity (fidelity mtime).
  implicit/*.pb  not read (not user conversations, semantics unknown); counted in notes.

Per conversation the best source wins: prompts from transcript.jsonl, else .db steps; responses from .db
gen_metadata (has usage), else transcript.jsonl; with neither, `activity` events at the .pb mtime and the brain
artifacts' updatedAt (fidelity "mtime"). Decoded .db timestamps must be plausible (2024-06-01 .. file mtime + 1
day) or the .db is discarded for that conversation and noted — the field map is second-hand.
The protobuf reader is a field decoder at fixed paths (not a scan for timestamp-looking bytes).
project: null (the workspace lives in state.vscdb trajectorySummaries protobuf, not read). interactive: null.

Check first on a real machine: coverage notes say which source each conversation used (transcript / db / mtime);
sessions > 0; the per-month histogram has no hole at the IDE v1 -> v2 switch; `.db` timestamps were not rejected.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

from cps_common import Coverage, Env, event, in_window, iter_jsonl, parse_time, usage

from ._sqlite_ro import open_ro

TOOL = "antigravity"
VERIFIED = False

ROOT_NAMES = ("antigravity", "antigravity-ide", "antigravity-cli", "antigravity-backup")
MIN_T = datetime(2024, 6, 1, tzinfo=timezone.utc).timestamp()
USER_STEP = 14


def roots(env: Env) -> List[Tuple[Path, Optional[Path]]]:
    """(root, conversations dir override)."""
    out = [(env.home / ".gemini" / n, None) for n in ROOT_NAMES]
    agy = env.environ.get("AGY_CONVERSATIONS_DIR")
    if agy:
        out.insert(3, (env.home / ".gemini" / "antigravity-cli", Path(agy)))
    return out


# ----------------------------------------------------------------------------- protobuf (wire format only)

def _varint(b: bytes, i: int) -> Tuple[Optional[int], int]:
    v, shift = 0, 0
    while i < len(b):
        x = b[i]
        i += 1
        v |= (x & 0x7F) << shift
        if not x & 0x80:
            return v, i
        shift += 7
        if shift > 70:
            break
    return None, i


def fields(b: bytes) -> List[Tuple[int, int, object]]:
    """[(field number, wire type, int or bytes)]; stops at the first malformed byte."""
    out, i = [], 0
    while i < len(b):
        key, i = _varint(b, i)
        if key is None or key >> 3 <= 0:
            break
        num, wt = key >> 3, key & 7
        if wt == 0:
            v, i = _varint(b, i)
            if v is None:
                break
            out.append((num, wt, v))
        elif wt == 1:
            if i + 8 > len(b):
                break
            out.append((num, wt, b[i:i + 8]))
            i += 8
        elif wt == 2:
            n, i = _varint(b, i)
            if n is None or i + n > len(b):
                break
            out.append((num, wt, b[i:i + n]))
            i += n
        elif wt == 5:
            if i + 4 > len(b):
                break
            out.append((num, wt, b[i:i + 4]))
            i += 4
        else:
            break
    return out


def first(fs, num: int):
    for n, _, v in fs:
        if n == num:
            return v
    return None


def _sub(fs, num: int) -> list:
    v = first(fs, num)
    return fields(v) if isinstance(v, bytes) else []


def _text(v) -> Optional[str]:
    if isinstance(v, bytes):
        try:
            return v.decode("utf-8")
        except UnicodeDecodeError:
            return None
    return None


def proto_time(v) -> Optional[float]:
    """A google.protobuf.Timestamp submessage, an ISO string, or a bare unix number (s or ms)."""
    if v is None:
        return None
    if isinstance(v, int):
        return v / 1000.0 if v >= 1e12 else float(v)
    s = _text(v)
    if s and s[:2] in ("19", "20") and "T" in s:
        return parse_time(s)
    ts = fields(v)
    sec = first(ts, 1)
    if isinstance(sec, int) and sec > 0:
        nanos = first(ts, 2)
        return sec + (nanos / 1e9 if isinstance(nanos, int) else 0.0)
    return None


def _int(v) -> Optional[int]:
    return v if isinstance(v, int) else None


def _blob(v) -> bytes:
    if isinstance(v, bytes):
        return v
    if isinstance(v, str):
        return v.encode("utf-8")
    return b""


def read_db(path: Path, cov: Coverage) -> Tuple[List[Tuple[float, Dict]], List[float]]:
    """-> ([(t, usage-and-model)] per model call, [t] per user prompt)."""
    calls, prompts = [], []
    with open_ro(path, cov) as con:
        if con is None:
            return calls, prompts
        try:
            rows = con.execute("SELECT idx, data FROM gen_metadata ORDER BY idx").fetchall()
        except Exception:
            rows = []
            cov.skip("db-no-gen_metadata")
        for _, data in rows:
            chat = _sub(fields(_blob(data)), 1)
            t = proto_time(first(_sub(chat, 9), 4))
            if t is None:
                continue
            u = _sub(chat, 4)
            i, o, cw, cr = (_int(first(u, n)) for n in (2, 3, 4, 5))
            uu = None
            if any((i, o, cw, cr)):
                uu = usage(i, o, cr, cw, None, None, (i or 0) + (o or 0) + (cw or 0) + (cr or 0))
            calls.append((t, {"usage": uu, "model": _text(first(chat, 19)) or _text(first(chat, 21))}))
        try:
            steps = con.execute("SELECT step_type, metadata FROM steps ORDER BY idx").fetchall()
        except Exception:
            steps = []
        for st, meta in steps:
            if st == USER_STEP:
                t = proto_time(first(fields(_blob(meta)), 1))
                if t is not None:
                    prompts.append(t)
    return calls, prompts


def read_transcript(path: Path, cov: Coverage) -> Tuple[List[Tuple[float, str]], List[float]]:
    """-> ([(t, prompt|auto_prompt)], [t of model responses])."""
    prompts, responses = [], []
    for r in iter_jsonl(path, cov):
        t = parse_time(r.get("created_at"))
        if t is None:
            continue
        typ, src = r.get("type"), r.get("source")
        if typ == "USER_INPUT":
            prompts.append((t, "prompt" if src == "USER_EXPLICIT" else "auto_prompt"))
        elif typ == "PLANNER_RESPONSE":
            responses.append(t)
    return prompts, responses


def _mtime(p: Path) -> Optional[float]:
    try:
        return p.stat().st_mtime
    except OSError:
        return None


# ----------------------------------------------------------------------------- collect

def _conversation(cid: str, db: Optional[Path], pb: Optional[Path], brain: Optional[Path], cov: Coverage,
                  used: Dict[str, int]) -> List[Tuple[float, str, Optional[Dict]]]:
    """[(t, kind, extra)] for one conversation from its best source."""
    out: List[Tuple[float, str, Optional[Dict]]] = []
    calls, db_prompts = [], []
    if db is not None:
        cov.files_read += 1
        calls, db_prompts = read_db(db, cov)
        hi = (_mtime(db) or 0) + 86400
        ts = [t for t, _ in calls] + db_prompts
        if ts and sum(1 for t in ts if MIN_T <= t <= hi) < len(ts):
            cov.note("some .db conversations had implausible decoded timestamps and were not used")
            used["db-rejected"] = used.get("db-rejected", 0) + 1
            calls, db_prompts = [], []
    tr = brain / ".system_generated" / "logs" / "transcript.jsonl" if brain else None
    t_prompts, t_resp = [], []
    if tr is not None and tr.is_file():
        cov.files_read += 1
        t_prompts, t_resp = read_transcript(tr, cov)
    if t_prompts:
        out += [(t, k, None) for t, k in t_prompts]
    else:
        out += [(t, "prompt", None) for t in db_prompts]
    if calls:
        out += [(t, "response", x) for t, x in calls]
    else:
        out += [(t, "response", None) for t in t_resp]
    if out:
        p_src = "transcript" if t_prompts else ("db" if db_prompts else "")
        r_src = "db" if calls else ("transcript" if t_resp else "")
        src = "+".join(sorted({p_src, r_src} - {""}))
        used[src] = used.get(src, 0) + 1
        return out
    # nothing readable at turn level: file times only
    if pb is not None:
        t = _mtime(pb)
        if t:
            out.append((t, "activity", None))
    if brain is not None:
        for m in brain.glob("*.metadata.json"):
            try:
                obj = json.loads(m.read_text(encoding="utf-8"))
            except (OSError, ValueError, UnicodeDecodeError):
                cov.parse_errors += 1
                continue
            t = parse_time(obj.get("updatedAt")) if isinstance(obj, dict) else None
            if t:
                out.append((t, "activity", None))
    if out:
        used["mtime"] = used.get("mtime", 0) + 1
    return out


def collect(env: Env, since, until, cov: Coverage) -> Iterator[Dict]:
    seen = set()
    used: Dict[str, int] = {}
    n_implicit = 0
    for root, conv_override in roots(env):
        conv_dir = conv_override or root / "conversations"
        brain_dir = root / "brain"
        for d in (conv_dir, brain_dir):
            cov.roots_checked.append(env.placeholder(d))
        if not conv_dir.is_dir() and not brain_dir.is_dir():
            continue
        cov.roots_found.append(env.placeholder(root))
        n_implicit += sum(1 for _ in (root / "implicit").glob("*.pb"))
        ids: Dict[str, Dict[str, Path]] = {}
        if conv_dir.is_dir():
            for p in conv_dir.iterdir():
                if p.suffix in (".db", ".pb") and p.is_file():
                    ids.setdefault(p.stem, {})[p.suffix] = p
        if brain_dir.is_dir():
            for p in brain_dir.iterdir():
                if p.is_dir():
                    ids.setdefault(p.name, {})["brain"] = p
        for cid in sorted(ids):
            if cid in seen:
                cov.skip("duplicate-conversation")
                continue
            seen.add(cid)
            f = ids[cid]
            for t, kind, x in sorted(_conversation(cid, f.get(".db"), f.get(".pb"), f.get("brain"), cov, used),
                                     key=lambda r: r[0]):
                if not in_window(t, since, until):
                    continue
                fid = "mtime" if kind == "activity" else "turn"
                yield event(TOOL, cid, t, kind, fidelity=fid, model=(x or {}).get("model"),
                            usage=(x or {}).get("usage") if kind == "response" else None)
    if used:
        cov.note("conversation sources: " + ", ".join(f"{k}={v}" for k, v in sorted(used.items())))
    if n_implicit:
        cov.note(f"{n_implicit} implicit/*.pb files not read (not user conversations)")
