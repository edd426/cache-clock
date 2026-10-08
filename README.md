# cache-clock

A Claude Code mod that shows how long your prompt cache has left, and acts just before it expires so that
coming back after a break doesn't re-write your whole conversation at full price.

```
cache ◷ 43m12s left              countdown to expiry (wall-clock based; sleep doesn't stop it)
cache ◷ 58m40s left · kept warm ×1
cache ● live                     a turn is running
cache ✕ expired · next prompt re-writes ~180k
cache ○ auto-compacted 415k→15k · next prompt writes ~15k
```

## Install

```
/plugin install cache-clock --marketplace edd426/cache-clock
```

Answer `y` to add the marketplace and pick the user scope. Requires a Claude Code build with function-hook
plugins (tested on 2.1.292). It runs only in interactive sessions: `claude -p` and SDK runs load it inert.

## What it does at the deadline

Every main-conversation request refreshes the cache for its TTL (1 hour on a subscription, 5 minutes on the API
default). Three minutes before it expires, with nobody typing, the mod looks at the context size:

| Context | Action |
|---|---|
| under 125k | keep-alive ping (a one-word fork that re-reads the cache), up to 3 times, then let it lapse |
| 125k – 300k | one ping, then compact at the next deadline |
| 300k and up | compact now |

A ping costs a cache read (0.1× input price). A compaction also reads the cache — it is a cache-sharing fork,
measured to read 414k of 415k cached tokens — and writes a short summary. Letting a 400k cache lapse costs a
2× re-write of all of it when you return. A deadline that passes while the machine sleeps can't be acted on;
it is logged as missed. `/cache-clock` shows the policy and everything the mod has done.

Settings (`/plugin` → cache-clock → configure): `ttl`, `autoAct` (off = countdown and a warning only),
`leadMinutes`, `keepAliveBelowTokens`, `maxKeepAlives`, `compactAboveTokens`.

Trade-off not priced: compaction loses conversation detail. If that matters more than tokens, raise
`compactAboveTokens` or turn `autoAct` off.

## Fit the thresholds to yourself: the cache-policy-study skill

The plugin ships a skill, `cache-policy-study`, that replays your own history — when you walk away, how long
you stay away, how big the context is when you leave — against every rule of the shape above and tells you
whether to keep the defaults or change them, with session-clustered bootstrap intervals, a temporal holdout and
sensitivity rows. It reads Claude Code, Codex, Gemini CLI, Antigravity, VS Code Copilot Chat, Cursor and
Copilot CLI history plus the OS sleep log (macOS verified; Windows and Linux readers untested), never calls a
model, and keeps prompt text, paths and project names out of its reports. Ask Claude something like *"run the
cache policy study"*, or directly:

```
python3 skills/cache-policy-study/scripts/study.py --out ./cache-policy-study
```

Only the Claude Code and Codex readers have been checked against real stores; the others are built from
format documentation and flagged as unverified in the report.

## How much it saves — and out of what

Percentages here are easy to misread, so with the denominator on each. On the author's history (476 idle
stretches of 55+ minutes, June–October 2026, costs in input-token equivalents at cache read 0.1×, 1-hour write
2×, output 5×):

| Saving of 116M tokens, divided by | Share |
|---|---|
| cost of returning to an expired cache (the gaps alone) | 69% |
| interactive conversations, main thread | 7.1% |
| interactive conversations including subagents | 3.9% |
| all Claude Code use, including scripted `-p` runs | 2.5% |

These are modelled from the history, not measured. One measured case: an unattended 415k session compacted
3 minutes before expiry cost ~178k on return instead of ~783k.

## Development

```
claude plugin validate .
claude plugin test .
python3 -m unittest discover -s skills/cache-policy-study/tests
```

MIT licensed.
