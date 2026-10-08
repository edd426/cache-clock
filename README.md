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

Answer `y` to add the marketplace and pick the user scope. Where the marketplace can't be reached (a work laptop, say), clone the
repo and install from the folder; the folder is read in place, so `git pull` plus `/reload-plugins` updates it:

```
git clone https://github.com/edd426/cache-clock
claude plugin marketplace add "$PWD/cache-clock"
claude plugin install cache-clock@cache-clock --scope user
echo '{"ttl":"5m"}' | claude plugin configure cache-clock@cache-clock --values-stdin
```

`--values-stdin` takes a JSON object of strings (`"true"`, `"2.5"`). On an API key or a gateway, set `ttl` to
`5m` as above: a mod can't see which kind of key is in use. Requires a Claude Code build with function-hook
plugins (tested on 2.1.292). It runs only in interactive sessions: `claude -p` and SDK runs load it inert.

## What it does at the deadline

Every main-conversation request refreshes the cache for its TTL (1 hour on a subscription, 5 minutes on an API
key, Bedrock, Vertex or Foundry, unless `promptCacheTtl` says otherwise). The mod works out which one applies
by itself: Claude Code's own settings and environment first (`promptCacheTtl`, the TTL variables, Bedrock,
Vertex or Foundry), then how the cache behaves — two returns within the hour that miss mean 5 minutes, one that
hits means an hour. `/cache-clock` says which it found. An API key in use is not visible to a mod, so on one
the clock assumes an hour until two returns have missed; set `ttl` to `5m` to skip that. Three minutes before it expires (30 seconds on a 5-minute TTL), with nobody typing, the mod
looks at the context size:

| Context | Action |
|---|---|
| under 125k | keep-alive ping (a one-word fork that re-reads the cache), up to 3 times, then let it lapse |
| 125k – 300k | one ping, then compact at the next deadline |
| 300k and up | compact now |

A ping costs a cache read (0.1× input price). A compaction also reads the cache — it is a cache-sharing fork,
measured to read 414k of 415k cached tokens — and writes a short summary. Letting a 400k cache lapse costs a
2× re-write of all of it when you return. A deadline that passes while the machine sleeps can't be acted on;
it is logged as missed. `/cache-clock` shows the policy and everything the mod has done.

## Turning it off for one conversation

Compaction replaces the conversation with a summary. When a conversation's detail matters more than the
tokens, switch the mod for that session (it lasts until the session ends and survives `/reload-plugins`):

| Command | What happens at the deadline |
|---|---|
| `/cache-clock off` | nothing — a warning that the cache is about to lapse; you pay the re-write when you return |
| `/cache-clock pings` | keep-alive pings only (up to `maxKeepAlives`), never a compaction: the context stays whole |
| `/cache-clock on` | back to the policy above |
| `/cache-clock` | what the mod has done, and this session's mode |

The status line ends in `· off` or `· pings only` while a session is switched. To turn it off everywhere, set
`autoAct` to false in the settings.

Settings (`/plugin` → cache-clock → configure): `ttl` (`auto` by default; `1h` or `5m` to pin it), `autoAct`
(off = countdown and a warning only), `leadMinutes` (minutes before expiry to act on a 1-hour cache, default 3),
`leadMinutes5m` (the same on a 5-minute cache, default 0.5 — at 4:30 of quiet), `keepAliveBelowTokens`,
`maxKeepAlives`, `compactAboveTokens`. `/cache-clock` shows each ping's round trip: if the slowest comes close to
the lead, or a ping shows MISSED, raise the lead.

## Fit the thresholds to yourself: the cache-policy-study skill

The plugin ships a skill, `cache-policy-study`, that replays your own history — when you walk away, how long
you stay away, how big the context is when you leave — against every rule of the shape above and tells you
whether to keep the defaults or change them, and which cache TTL to use, with session-clustered bootstrap intervals, a temporal holdout and
sensitivity rows. It reads Claude Code, Codex, Gemini CLI, Antigravity, VS Code Copilot Chat, Cursor and
Copilot CLI history plus the OS sleep log (macOS and Windows verified; Linux untested), never calls a
model, and keeps prompt text, paths and project names out of its reports. Every saving it reports comes with its share of
your total spend, as in the table below. Ask Claude something like *"run the
cache policy study"*, or directly:

```
python3 skills/cache-policy-study/scripts/study.py --out ./cache-policy-study
```

Checked against real stores: Claude Code and Codex (macOS and Windows), Gemini CLI, Cursor, VS Code Copilot Chat
and Copilot CLI (Windows 11). Antigravity is partial: its times read, but its model calls carry no timestamp, so
its tokens are not counted yet. On Windows `python3` is often the Microsoft Store stub — use `python` or `py -3`.

## 1 hour or 5 minutes?

A 1-hour cache write costs 2× the input price instead of 1.25×. It pays for itself when requests come back
5–60 minutes apart often enough. Claude Code lets you choose separately for conversations (`promptCacheTtl`
in `~/.claude/settings.json`, or `CLAUDE_CODE_PROMPT_CACHE_TTL` for one run) and for subagents
(`subagentPromptCacheTtl`). The study answers each one separately from your history, request by request, with
cache-clock layered on the interactive conversations. On the author's history:

| Traffic | Now | 1h vs 5m | Verdict |
|---|---|---:|---|
| interactive conversations (with cache-clock on both) | 1h | **−7.9%** (90% −9.9% to −5.8%) | keep 1 hour |
| interactive conversations, no mod | 1h | −32% (90% −35% to −27%) | keep 1 hour |
| scripted runs (`claude -p`, SDK) | 1h | **+7.9%** (90% +6.7% to +9.1%) | 5 minutes: requests come seconds apart, so the hour buys nothing |
| subagents | 5m | −2.7% (90% −9.3% to +6.0%) | either; the sign flips with how much context survives |

Costs use API list-price ratios (1-hour write 2×); whether a subscription's usage meter weighs them the same way
is not established. A model check runs the formulas blind at the TTL the history actually used: here they land
within 0.3–2.0% of the observed cost.

On a 5-minute TTL the mod still saves 31% of interactive cost on this history, acting 30 seconds before expiry.
Acting at 2:30 instead — the old default — pinged and compacted for people who were about to come back; on a
5-minute work history that halved the saving (24% vs 43% of idle-gap cost). Large conversations are still
compacted after 4.5 quiet minutes, which loses detail; `/cache-clock pings` keeps them whole and saves 17% here.
If your plan lets you set `"promptCacheTtl": "1h"`, that was the cheaper fix on this history.

## How much it saves — and out of what

A percentage means little without its denominator, and the study reports the saving against each one. On
the author's history (480 idle stretches of 55+ minutes, June–October 2026; costs in input-token equivalents at
cache read 0.1×, 1-hour write 2×, output 5×) the mod saves 116M:

| Divided by | Total | Share | From the row above |
|---|---:|---:|---:|
| the cost of returning to an expired cache (the idle gaps alone) | 169M | **69%** | |
| interactive Claude Code, main thread | 1.63B | 7.1% | × 0.10 |
| + their subagents | 2.93B | 4.0% | × 0.56 |
| + scripted `claude -p` / SDK runs | 4.64B | 2.5% | × 0.63 |
| + other AI tools (here: Codex) | 4.83B | **2.4%** | × 0.96 |

Each share is the headline × gap cost ÷ that total: the gaps are 10% of main-thread spend, so 69% of them is
7.1% of it, and so on down. The mod can only act on the first row; subagents and scripted runs have no one to
walk away. These are modelled from the history, not measured. One measured case: an unattended 415k session compacted
3 minutes before expiry cost ~178k on return instead of ~783k.

## Development

```
claude plugin validate .
claude plugin test .
python3 -m unittest discover -s skills/cache-policy-study/tests
```

## License

[MIT](LICENSE).
