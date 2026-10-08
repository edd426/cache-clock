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
9. **Confidence.** high: n ≥ 200, interval ≤ 12 points, holdout gap ≤ 3 points, near-optimal in ≥ 70% of
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
| independence (global view) | return time does not depend on context size | compare with the Claude Code view |
| **not priced** | follow-up requests after a compaction re-read ~60k instead of C (favours compaction, so the study is conservative toward pings); compaction loses conversation detail (favours pings) | — |

## Verified live (2026-10-07, Claude Code 2.1.292, Haiku 4.5, 5-minute TTL forced with `FORCE_PROMPT_CACHING_5M=1`)

| mechanism | test | result |
|---|---|---|
| compaction reads the cache | `/compact` on a 22k-token session, a mod logging the compaction request's usage | read 21,874 cached of 21,874; 1,477 uncached; 185 written |
| keep-alive ping between turns | fork ping at 4.5 min, next message at 9 min; control without ping | ping session read 22,245 / wrote 37; control read 14,160 / wrote 8,176 (miss) |
| keep-alive ping while a turn runs | fork ping at 2.5 min during a 400 s tool call; control without ping | ping read 22,614; post-tool request read 22,731 / wrote 331; control read 14,159 / wrote 8,658 (miss) |
| shared prefix after a miss | the controls above | ~14k (system prompt and tools) stays cached across sessions — the study's warm-prefix term |
| mod auto-compaction, production scale | cache-clock 0.3.0 unattended on a 415k Opus 5.5 session (1-hour TTL), user back 1 h 48 min after the last response | fired at T−3:00 (0.3 s late); read 414,026 cached of 414,638, wrote 518, summary 9,951 output; first request back read 25,488 / wrote 40,461 (post ctx 66k ≈ the 60k default; summary ≈ 0.68 × postTokens, above the 0.5 assumed). ≈178k input-equivalents vs ≈783k for letting it lapse (−77%); a ping would have cost ≈83k here because the return came within the ping's hour |
