# Event schema — the contract between adapters and the analysis

Every adapter turns one tool's local history into a stream of **events**: one JSON object per line in
`events.jsonl`. The analysis reads nothing else. An adapter never writes to a tool's store, never prints
prompt or response text, and never emits a raw filesystem path or project name (use `project_id()`).

| field | type | meaning |
|---|---|---|
| `tool` | str | registry id: `claude-code`, `codex`, `vscode-copilot`, `copilot-cli`, `gemini-cli`, `antigravity`, `cursor` |
| `session` | str | the tool's own session id (unique within the tool) |
| `t` | float | UTC epoch **seconds** of the event |
| `kind` | str | see below |
| `interactive` | bool or null | false for headless/scripted runs (`claude -p` = entrypoint `sdk-*`; `codex exec` = originator `codex_exec`); null when unknown |
| `fidelity` | str | `turn` (per-message timestamps), `session` (start/end only), `mtime` (file modification time only) |
| `project` | str or null | `project_id(cwd_or_workspace)` — a 10-char hash, never the path |
| `model` | str or null | model id as the store spells it |
| `subagent` | bool | true for a subagent/sidechain thread (excluded from the main timeline) |
| `usage` | object or null | on `response` only; every member may be null when the store lacks it: `input`, `output`, `cache_read`, `cache_write`, `cache_write_5m`, `cache_write_1h`, `ctx` |
| `compaction` | object or null | on `compaction` only: `pre`, `post` (tokens), `trigger` (`manual`/`auto`/null) |

`kind` values:

- `prompt` — a human typed something (the person was present at `t`)
- `auto_prompt` — the conversation was resumed by something other than a person (task notification,
  scheduled wake-up, loop tick, slash-command expansion); the person was **not** necessarily present
- `response` — one completed model response (deduplicated: one per API response, not one per content block)
- `turn_end` — the agent finished its turn and is waiting for the person (between two `turn_end`s the agent may
  sit in a long tool run or a permission wait: it is not idle, and a compaction is refused there). Emitted only
  by stores that record it (Claude Code `turn_duration` rows); the analysis falls back to "no prompt in between"
- `session_start`, `session_end` — bounds when only those are known
- `activity` — the person or tool was active at `t`, nothing more is known (e.g. a file mtime)
- `compaction` — the conversation was compacted

`usage.ctx` is the size of the prompt the **next** request re-sends: `input + cache_read + cache_write + output`
when all are known (Claude Code); the adapter leaves it null rather than guess.

**spend.json** (written by collect.py, not by adapters): every `response` any adapter yielded — subagent and
scripted-run ones included — summed per `tool` × `class` (`main` = interactive or unknown main thread,
`subagent`, `headless` = scripted runs and their subagents) × UTC `day`: `responses`, `with_usage`, and token
sums `input`, `output`, `cache_read`, `cache_write_5m`, `cache_write_1h`, `cache_write_other` (writes the store
did not split by TTL). `events.jsonl` then drops subagent events (they feed no behaviour) and, unless
`--include-headless`, scripted-run events. The Claude Code adapter reads `<session>/subagents/*.jsonl` for
responses only, for this ledger.

**ttl.jsonl** (written by collect.py): every Claude Code `response` and `compaction` in all three classes, for
the TTL comparison — `lane` (the session id; a subagent's is `session/agent`), `session`, `class` (`main`,
`headless`, `subagent` — here a scripted run's subagent is `subagent`, since subagents have their own TTL
setting), `t`, `kind`, `model`, `usage`. Subagent responses carry `lane` from the adapter.

Coverage: each adapter also fills a `Coverage` record (see `cps_common.Coverage`): roots it checked (as
placeholders like `{HOME}/.codex/sessions`), files read, files skipped with a reason, parse errors, and notes.
`collect.py` adds sessions, events, first/last date and a per-month session histogram per tool — the month
histogram is the guard against a store with two formats where a reader silently sees only one.

## Per-tool notes (2026-10-07, from the adapter builds)

- **Token mapping.** OpenAI (Codex) and Gemini report `input` *including* cached tokens; their adapters emit
  `input = input − cached`, `cache_read = cached`, `ctx = total input + output` (Gemini adds thoughts to output).
  Claude Code reports the three parts separately. `cache_write` is null where the vendor has no write charge.
- **Cursor** emits one `response` per assistant bubble, which is one per tool step, not one per API call —
  response counts are inflated; use Cursor for presence and timing, not for request counts. Its workspace
  `aiService.generations` become `activity` events with `fidelity="turn"` in a pseudo-session per workspace.
  Token counts in the store are almost always 0.
- **Gemini CLI** cannot tell `gemini -p` runs from interactive ones: `interactive` is null for every session.
- **Antigravity** legacy `conversations/*.pb` files are encrypted: those conversations only yield `activity`
  events with `fidelity="mtime"`. Newer `.db` stores and `brain/*/transcript.jsonl` give per-turn times.
- **sleep.json** `reason` labels ending in `(to DarkWake)` (lid closed, but something kept the system up: user
  processes may have kept running) or `(log start)` (the log begins mid-sleep: the true start is earlier) mark
  weaker evidence. Intervals overlapping the window are returned whole.
