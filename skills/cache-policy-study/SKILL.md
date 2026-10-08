---
name: cache-policy-study
description: Studies how the user actually works with AI coding tools on this machine (when they walk away, how long they stay away, how big the conversation is when they leave) and derives the cheapest prompt-cache policy for Claude Code - when to send keep-alive pings, when to compact, and when to let the cache lapse - with thresholds ready to paste into the cache-clock mod. Reads local history from Claude Code, Codex, Gemini CLI, Antigravity, VS Code Copilot Chat, Cursor and Copilot CLI, plus the OS sleep log, and reports two answers - Claude Code behaviour alone, and global behaviour across all tools - with bootstrap intervals, a temporal holdout and sensitivity checks. Use when the user asks how to avoid cache misses or cache re-writes after stepping away, what keep-alive or auto-compact thresholds to use, whether keep-alive pings are worth it, how often they return within an hour or two, how big their context is when they walk away, or wants this study run on another machine (including a Windows work laptop).
---

# Cache policy study

A read-only, offline study of the user's own AI-tool history. It never calls a model, never writes into
any tool's store, and never puts prompt text, paths, project names or session ids into its reports.

## Quickstart

```bash
SKILL="${CLAUDE_SKILL_DIR:-<directory holding this SKILL.md>}"
python3 "$SKILL/scripts/study.py" --out "./cache-policy-study-$(date +%F)"
```

Windows (PowerShell): `python "$env:SKILL\scripts\study.py" --out ".\cache-policy-study-$(Get-Date -Format yyyy-MM-dd)"`.

It runs three steps, each runnable alone: `collect.py` (history stores → `events.jsonl`, `coverage.json`,
`sleep.json`), `analyze.py --run DIR` (→ `results.json`), `report.py --run DIR` (→ `report.html`,
`summary.md`, `photo.html`). Python 3.9+, standard library only. Useful `collect.py` flags: `--only
claude-code,codex`, `--since 2026-06-01`, `--include-headless` (keep scripted runs; they are excluded from
behaviour by default but always counted).

## Before you believe the numbers — check, in this order

1. **Coverage** (`report.html` → Data coverage). Every tool the user uses should be `ok` with a plausible
   per-month histogram. A sudden cut-off month usually means a store with two formats where one was missed
   (it happened before with VS Code Copilot). A tool marked *unverified adapter* was built from public
   format documentation, not checked against a real store: if its numbers look wrong, inspect the store
   (`python3 scripts/collect.py --only <tool> --out /tmp/x` and read `coverage.json` notes) before using them.
2. **TTL validation.** Misses should be near 0% below the TTL and near 100% above it. If not, the cache model
   does not match this user's plan and the savings are not trustworthy.
3. **Sample size and confidence label** on each recommendation. Low confidence = report the rule as
   provisional and say what more data would settle it.

## Reading the result

Each view gives a **verdict** against the rule the user runs now (their cache-clock settings, else the mod
defaults; the report says which): **keep** unless a session-clustered paired bootstrap shows the best rule's
gain above zero with a median of at least 1 point, in which case **change**. On "keep", the best rule is shown
as an equivalent alternative, not a recommendation. Settings are exact cache-clock keys (`ttl`, `leadMinutes`,
`keepAliveBelowTokens`, `maxKeepAlives`, `compactAboveTokens`); the lead is an input (`--lead`), never fitted.

- **Claude Code** — fitted to Claude Code idle stretches. The primary answer.
- **Fallback: global (x% Claude Code, y% other tools)** — return times pooled across every AI tool, applied
  to Claude Code's context sizes. Use it only when Claude Code history is thin; it is mostly the same data,
  not independent evidence, so never present the two headlines as two confirmations. The presence section
  says whether stepping into another AI tool predicts a different return — report a presence-aware mod as a
  direction, not something already built.

Lead the answer to the user with at most five short lines: the verdict and rule, its expected saving with
interval, and the one caveat that matters most. Charts and tables over prose; put detail below that. Always
say what is not priced: compaction loses conversation detail, and the cheaper follow-up requests after a
compaction are not counted either (the study is conservative toward pings).

## Running it on another machine

Copy this whole folder (it has no dependencies) and run the quickstart there. When the only channel back is
a photo, photograph `photo.html` — one screen, large type, no identifying strings. Keep the full report on
that machine.

## Files

- `scripts/collect.py`, `scripts/adapters/*.py` (one per tool), `scripts/power.py` (sleep/wake log)
- `scripts/analyze.py`, `scripts/report.py`, `scripts/study.py`
- `references/event-schema.md` (the adapter contract), `references/method.md` (method and assumptions),
  `references/pricing.json` (cost multipliers — edit for other prices)
- `tests/` — `python3 -m unittest discover -s tests`
