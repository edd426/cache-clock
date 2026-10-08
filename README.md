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

Settings (`/plugin` → cache-clock → configure): `ttl`, `autoAct` (off = countdown and a warning only),
`leadMinutes`, `keepAliveBelowTokens`, `maxKeepAlives`, `compactAboveTokens`.

## Fit the thresholds to yourself: the cache-policy-study skill

The plugin ships a skill, `cache-policy-study`, that replays your own history — when you walk away, how long
you stay away, how big the context is when you leave — against every rule of the shape above and tells you
whether to keep the defaults or change them, with session-clustered bootstrap intervals, a temporal holdout and
sensitivity rows. It reads Claude Code, Codex, Gemini CLI, Antigravity, VS Code Copilot Chat, Cursor and
Copilot CLI history plus the OS sleep log (macOS verified; Windows and Linux readers untested), never calls a
model, and keeps prompt text, paths and project names out of its reports. Every saving it reports comes with its share of
your total spend, as in the table below. Ask Claude something like *"run the
cache policy study"*, or directly:

```
python3 skills/cache-policy-study/scripts/study.py --out ./cache-policy-study
```

Only the Claude Code and Codex readers have been checked against real stores; the others are built from
format documentation and flagged as unverified in the report.

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

Dual-licensed under [MIT](LICENSE-MIT) or [Apache 2.0](LICENSE-APACHE), at your option — the two licenses
most company open-source policies pre-approve; Apache 2.0 adds an explicit patent grant.
