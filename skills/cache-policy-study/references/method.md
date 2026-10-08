# Method

**Question.** Claude Code caches the conversation server-side for a TTL (1 hour on a subscription, 5 minutes
elsewhere). Walk away longer than that and your next prompt re-writes the whole context at the cache-write
price. The cache-clock mod can act a few minutes before expiry: send a **keep-alive ping** (re-reads the cached
context at 0.1× and restarts the TTL), **compact** (reads the context from cache, writes a summary, and the next
prompt writes only the small post-compaction context), or do nothing. Should you keep the rule you run now, or is
another one clearly cheaper *for how you actually work*?

**Data.** `events.jsonl` from `collect.py`: one row per prompt, response, turn end, compaction or activity, per
tool. No prompt text, paths or project names reach the analysis.

## Steps

1. **Current rule and lead.** Your cache-clock settings are read from Claude Code's `settings.json`
   (`pluginConfigs` → `cache-clock*` → `options`); without them, the mod defaults (<125k up to 3 pings, 125–300k
   one ping then compact, ≥300k compact, 3 min early). The lead is an **input** (`--lead`, default your setting or
   3): the model has no cost for acting late, so searching over it always picks the shortest lead, while the mod
   measures its deadline from the *end* of a request and a slow request leaves less margin than the lead says.
   Leads 2/3/5 appear only as sensitivity rows.
2. **TTL.** Each Claude Code session's TTL comes from which cache-write field it uses. The dominant TTL is analysed;
   sessions on the other one are excluded and counted; `--ttl` forces one. *Validation:* a "miss" is the next
   request reading under half of the previous context from cache. Misses should be rare before the TTL and
   near-certain after it; the bucket just past the TTL is mixed because a request is timed at its first
   transcript row. The median cache read on cold misses is the **shared prefix W** (system prompt and tools,
   kept warm by other sessions): a cold re-send costs `write × (C − W) + 0.1 × W`, and so does the
   post-compaction write.
3. **Idle stretches.** Every gap between consecutive requests in an interactive, non-subagent session of at
   least TTL − 5 min. Context at walk-away C = the previous request's prompt size. A manual `/compact` inside the
   gap ends the stretch at the compaction (you were there; the mod resets on any compaction), and that return is
   priced as your compaction — paid in every policy (0.1×C warm, 1.0×C uncached cold, + summary + post-compaction
   write) unless the mod had already compacted, which makes it moot. A session's last
   request becomes "never came back" when the data ends more than 4 TTLs later; otherwise it is dropped
   (right-censored); a session ended by a command after its last request (e.g. `/exit`) is dropped too.
   **Turn state:** a `turn_end` (Claude Code's `turn_duration` row) dates the end of the turn. That row exists for
   only about half of all turns, so its absence is not evidence: a prompt or automatic prompt before the next
   request also means the turn had ended. No turn end and no prompt means the next request answered a tool
   result or a permission — the turn was running ("within a turn"). A return on a different model is always cold.
4. **Cost of one stretch** under the mod's own schedule: first action at TTL − lead; after a successful ping at p
   the next at p + TTL − lead (57, 114, 171 min for 1 h / 3 min). Ping = 0.1×C + ping output; compaction =
   0.1×C + summary output, then the return writes the post-compaction context. **Mid-turn the engine refuses a
   compaction:** the mod pings instead and keeps following the rule at the next deadline, up to maxKeepAlives
   pings in the stretch, then lets the cache lapse. A return before the next action ends the sequence: warm →
   0.1×C, cold → the cold re-send. Never-returned stretches pay only the actions (the CLI is assumed open).
5. **Search.** Per context band (merged until n ≥ 30), the cheapest of {0–5 pings} ∪ {0–3 pings then compact}.
   Then the best rule the mod runs (`decide()`): below A, up to N pings; A–B, one ping then compact; above B,
   compact — over A ∈ {0, 50k … 200k}, B ∈ {150k … 600k, never}, N ∈ 1–4, plus your current rule.
6. **Verdict.** A session-clustered bootstrap (1,000 resamples of *sessions*, fixed seed). The best rule on the full
   data is the maximum over ~200 rules, so its in-sample gain is optimistic. The verdict therefore re-selects the
   best rule in each resample and scores its gain over your current rule on the sessions that resample left out
   (out-of-bag). **Change** only if that gain's 90% interval is above 0 *and* its median is at least 1 point;
   otherwise **keep your current rule**. The best rule is still shown: "equivalent" when the out-of-sample interval
   includes 0, "better by ~x pts, below the 1-point bar" when it does not. The same resamples give the 90%
   intervals and the share each rule wins. **Confidence is sampling confidence only**: the report puts the range
   over the assumption rows (and the compaction-uncached case) right under the recommendation.
   Temporal holdout: choose on the earliest 70%, score on the latest 30%. Sensitivity rows re-score under other
   assumptions (below).
7. **Sleep.** With `sleep.json`, an action is lost when the machine sleeps through its whole lead window (the mod
   still acts if it wakes before expiry), and nothing later happens in that stretch. Only stretches inside the
   power log's coverage are used (`window` in sleep.json, else first sleep record → collection time). Lid-closed
   spans held awake by an assertion ("to DarkWake") are not counted as asleep; a sensitivity line counts them.
8. **Global view.** *(i) Presence:* a Claude Code stretch is "elsewhere" if another AI tool shows activity between
   walk-away and the first action; one rule per class vs one rule says whether the mod should watch other tools
   (in-sample, so an upper bound). *(ii) Pooled returns:* in every tool, a gap that ends in a typed `prompt`
   (minus the session's previous event), plus never-returned sessions, paired with Claude Code's contexts
   (assumed independent) and taken as between turns. It is labelled with its data mix (e.g. "global (92% Claude
   Code, 8% Codex)"): a fallback for thin Claude Code history, not independent evidence. Keeping only
   prompt-ended returns but every never-returned session inflates "never" — a selection effect toward fewer pings.
9. **Out of what.** The saving (do-nothing cost − the recommended rule's cost, in input-token equivalents) is
   divided by ever-wider totals over the Claude Code timeline's window (first to last interactive main-thread
   response, by UTC day): the idle-gap cost itself (the headline), the interactive main thread, + its
   subagents, + scripted runs (`claude -p`, SDK, and their subagents), + every other AI tool with token counts.
   Each share = headline × gap cost ÷ that total, so each step shrinks the share by the ratio of the two totals;
   the 90% interval is the headline's, scaled the same way. Totals come from `spend.json` (every response
   collect.py read, before it dropped subagent and scripted-run events from `events.jsonl`); without it only
   the main thread is counted. Other vendors' tokens are weighted with the same price ratios unless
   `pricing.json` → `per_tool` overrides them; stores without token counts are named and left out.
10. **Which TTL** (`ttl.py`, from `ttl.jsonl`). Claude Code sets the TTL separately for the main conversation
   (`promptCacheTtl`, interactive and `-p` alike; `CLAUDE_CODE_PROMPT_CACHE_TTL` wins, so a scripted run can set
   its own) and for subagents, workflows and helpers (`subagentPromptCacheTtl`); `FORCE_PROMPT_CACHING_5M=1`
   overrides both, `ENABLE_PROMPT_CACHING_1H=1` turns both to 1 hour, `CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL`
   overrides the subagent setting and an agent's own definition can name one (read from strings in the CLI
   2.1.293 binary). Helper and side requests follow the subagent setting but leave no transcript, so the
   subagent lane does not count them. Unset,
   the main conversation is 1 hour on a subscription within its limits and 5 minutes on an API key, Bedrock,
   Vertex or Foundry; subagents are 5 minutes. So the comparison runs per lane class — interactive
   conversations, scripted runs, subagents — over every request with token counts, one lane per conversation or
   subagent, walked in order. A lane restarts at its first request, after a compaction and on a model switch.
   For each request, both TTLs:
   - under 5 minutes since the lane's previous request: the observed read, the rest written;
   - 5–60 minutes: the 1-hour TTL reads the observed read if the history ran at 1 hour, else f × min(previous,
     current cacheable context) — f, the prefix survival, is measured on the person's own 1-hour returns
     (token-weighted, ≥ 30 returns) else 0.8, with 0.5/0.8/1.0 shown as scenarios. The 5-minute TTL reads the
     observed read if the history ran at 5 minutes, else only the shared prefix, and only if another lane of the
     same class and model sent a request within 5 minutes;
   - a lane's first request, or over 60 minutes: the shared prefix (a first request's own observed read; capped
     at the class's median first-request read otherwise, since a warm read past an hour may be cache-clock's
     keep-alive) when another lane ran within that TTL, else nothing. A request observed at 5 minutes keeps its
     read under both TTLs — a lower bound for the hour.
   Writes are the cacheable context minus the read, at 1.25× or 2×. On the interactive lane the mod is layered
   on: analyze.py's own stretch model is rerun with every interactive session at each TTL (lead capped at half
   the TTL, so 2.5 min at 5 minutes) and its saving per session added for three variants — your rule, the best
   rule for that TTL, and pings only (`/cache-clock pings`). The verdict compares the best rule at each TTL
   (no mod on the other lanes) with a session-clustered bootstrap (90%); a switch needs the interval clear of
   zero and ≥ 1% of that lane's spend, and — where f enters at all — the same sign and size under f = 0.5, 0.8
   and 1.0, else "either, depends on f". Checks shown: the **model check** — the formulas applied blind at the
   observed TTL (observed reads ignored outside the under-5-minute band) against the observed cost; it can fail,
   and a gap of more than a few % means the verdict's other-TTL side is not trustworthy — and the reuse ratio — extra cache reads the hour buys ÷ the tokens it writes at 2× — against the 65.2% break-even
   ((2 − 1.25) ÷ (1.25 − 0.1)).
11. **Confidence.** high: n ≥ 200, interval ≤ 12 points, holdout gap ≤ 3 points, near-optimal in ≥ 70% of
   resamples. low: n < 50, interval > 25 points, or holdout gap > 10. Otherwise medium; the global view is
   capped at medium.

## Assumptions

| assumption | value / where | if wrong (sensitivity row) |
|---|---|---|
| prices | `pricing.json`: read 0.1×, 5-min write 1.25×, 1-h write 2×, output 5× | the other TTL's write price |
| compaction reads the cache | verified live 2026-10-07 | "compaction uncached" — the saving collapses |
| post-compaction context | median ctx of your first request after a compaction; 60k default | ×2 |
| summary output | 0.5 × median `postTokens` (no summary length in events; one live measurement, ~10 transcripts); 7k default | ×2 |
| ping cost | 0.1×C + 60 output tokens; a fork may inherit thinking settings (unmeasured) | 200 tokens |
| shared prefix W | median cache read on cold misses | W = 0 |
| lead | an input; late pings (slow requests) are not priced | lead 2 and 5 |
| never came back | CLI still open, so the mod acts | cost nothing |
| mid-turn | inferred from turn ends and prompts; compaction refused → pings. A transcript check that pairs each walk-away tool_use with its tool_result agreed on 471 of 476 stretches and moved the current rule from 71.3% to 70.8% (skeptic re-check, before manual-/compact pricing) | mid-turn ignored |
| manual /compact return | priced as your own compaction | priced as an ordinary request |
| other tools' price ratios | Claude's (read 0.1×, output 5×) unless `per_tool` in pricing.json | — (stated next to the share) |
| independence (global view) | return time does not depend on context size | compare with the Claude Code view |
| prefix survival f (TTL step) | measured on your 1-hour returns, else 0.8; only requests observed at 5 min use it | f = 0.5 / 0.8 / 1.0 |
| shared prefix across lanes (TTL step) | a new lane reads another's cached system prompt and tools only if one of its class and model ran within the TTL | — (asymmetric: a 5-min-observed read is kept under 1 h, a lower bound) |
| price ratios (TTL step) | API list prices; whether a subscription's usage meter weighs 1-hour writes at 2× is not established | — (stated in the report) |
| compaction requests (TTL step) | not in the pair model's totals; the mod layer's compaction savings are priced against them, almost equally at both TTLs | — |
| **not priced** | follow-up requests after a compaction re-read ~60k instead of C (favours compaction, so the study is conservative toward pings); compaction loses conversation detail (favours pings) | — |

## Verified live (2026-10-07, Claude Code 2.1.292, Haiku 4.5, 5-minute TTL forced with `FORCE_PROMPT_CACHING_5M=1`)

| mechanism | test | result |
|---|---|---|
| compaction reads the cache | `/compact` on a 22k-token session, a mod logging the compaction request's usage | read 21,874 cached of 21,874; 1,477 uncached; 185 written |
| keep-alive ping between turns | fork ping at 4.5 min, next message at 9 min; control without ping | ping session read 22,245 / wrote 37; control read 14,160 / wrote 8,176 (miss) |
| keep-alive ping while a turn runs | fork ping at 2.5 min during a 400 s tool call; control without ping | ping read 22,614; post-tool request read 22,731 / wrote 331; control read 14,159 / wrote 8,658 (miss) |
| shared prefix after a miss | the controls above | ~14k (system prompt and tools) stays cached across sessions — the study's warm-prefix term |
| mod auto-compaction, production scale | cache-clock 0.3.0 unattended on a 415k Opus 5.5 session (1-hour TTL), user back 1 h 48 min after the last response | fired at T−3:00 (0.3 s late); read 414,026 cached of 414,638, wrote 518, summary 9,951 output; first request back read 25,488 / wrote 40,461 (post ctx 66k ≈ the 60k default; summary ≈ 0.68 × postTokens, above the 0.5 assumed). ≈178k input-equivalents vs ≈783k for letting it lapse (−77%); a ping would have cost ≈83k here because the return came within the ping's hour |
