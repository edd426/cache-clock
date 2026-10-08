import { atom, read, update } from 'claude-code'
import type { EngineInterface, ModelUsage, Register } from 'claude-code'

import type { CacheTouch, Compaction, LedgerEntry, Mode, Warmth } from '../types'

// Every main-thread request re-reads (and so refreshes) or re-writes the cached
// prefix, so the clock restarts on each one. Subagent requests carry their own
// prefixes and leave the main thread's entry alone. A keep-alive ping is a fork
// of the main thread's last request: it reads the same prefix and refreshes it.
const last = atom({ plugin: 'cache-clock', key: 'last' } as const, null)
const compacted = atom({ plugin: 'cache-clock', key: 'compacted' } as const, null)
const warm = atom({ plugin: 'cache-clock', key: 'warm' } as const, null)
// Kept in session state, not a module variable, so a plugin reload cannot quietly switch it back on.
const mode = atom({ plugin: 'cache-clock', key: 'mode' } as const, null)

const TTL_MS = { '1h': 60 * 60 * 1000, '5m': 5 * 60 * 1000 } as const
const LEDGER = 'ledger'
const PING_PROMPT = 'Keep-alive check from the cache-clock plugin. Reply with only: ok'

export const fmt = (ms: number) => {
  const s = Math.max(0, Math.ceil(ms / 1000))
  const m = Math.floor(s / 60)
  return m >= 60 ? `${Math.floor(m / 60)}h${String(m % 60).padStart(2, '0')}m` : `${m}m${String(s % 60).padStart(2, '0')}s`
}

const k = (tokens: number) => (tokens >= 1e6 ? `${(tokens / 1e6).toFixed(1)}M` : `${Math.round(tokens / 1000)}k`)

export type Policy = { keepAliveBelow: number; maxKeepAlives: number; compactAbove: number }
export type Action = 'ping' | 'compact' | 'lapse'

// The defaults come from the cache-policy-study skill (skills/cache-policy-study), which replays
// a person's own idle stretches against every rule of this shape and says whether to change them.
export const decide = (ctx: number, pingsDone: number, p: Policy): Action => {
  if (ctx >= p.compactAbove) return 'compact'
  if (ctx >= p.keepAliveBelow) return pingsDone < 1 ? 'ping' : 'compact'
  return pingsDone < p.maxKeepAlives ? 'ping' : 'lapse'
}

const MODE_TAG: Record<Mode, string> = { auto: '', pings: ' · pings only', off: ' · off' }

export const line = (touch: CacheTouch | null, w: Warmth | null, done: Compaction | null, now: number, ttlMs: number, isBusy: boolean, contextTokens?: number, m: Mode = 'auto') =>
  bare(touch, w, done, now, ttlMs, isBusy, contextTokens) + MODE_TAG[m]

const bare = (touch: CacheTouch | null, w: Warmth | null, done: Compaction | null, now: number, ttlMs: number, isBusy: boolean, contextTokens?: number) => {
  if (isBusy) return 'cache ● live'
  if (touch === null) {
    if (done) return `cache ○ ${done.isAuto ? 'auto-' : ''}compacted ${k(done.before)}→${k(done.after)} · next prompt writes ~${k(done.after)}`
    return 'cache ○ cold'
  }
  const left = (w?.at ?? touch.at) + ttlMs - now
  if (left <= 0) return `cache ✕ expired · next prompt re-writes ~${k(contextTokens ?? touch.ctx)}`
  return `cache ◷ ${fmt(left)} left${w ? ` · kept warm ×${w.pings}` : ''}`
}

const ctxOf = (u: ModelUsage) => u.input_tokens + u.cache_read_input_tokens + u.cache_creation_input_tokens + u.output_tokens

// `turns` holds the main-thread turns in flight: between a turn's model steps (a long tool run, a
// permission wait) the engine refuses a compaction, so the mod pings instead and decides again next deadline.
type Ttl = keyof typeof TTL_MS
type Clock = Policy & { ttl: Ttl; ttlSource: string; ttlFixed: boolean; misses: number; resumedAt?: number; ttlMs: number; leadMs: number; leadMinutes: number; leadMinutes5m: number; autoAct: boolean; isLive: boolean; busy: number; acting: boolean; actedFor: number; turns: Set<string> }

async function record($: EngineInterface, entry: LedgerEntry) {
  const prev = ((await $.store.get(LEDGER)) as LedgerEntry[] | undefined) ?? []
  await $.store.set(LEDGER, [...prev, entry].slice(-500))
}

async function ping($: EngineInterface, ctx: number, w: Warmth | null, why?: string) {
  const now = await $.clock.now()
  const r = await $.model.fork({ prompt: PING_PROMPT })
  const ms = (await $.clock.now()) - now
  const u = 'usage' in r ? r.usage : undefined
  // A ping that read under half the context did not find the entry: it lapsed.
  const ok = r.isAnswered && u !== undefined && u.cache_read_input_tokens >= ctx * 0.5
  await record($, {
    at: now, kind: 'ping', ctx, ok,
    read: u?.cache_read_input_tokens, written: u?.cache_creation_input_tokens, input: u?.input_tokens, output: u?.output_tokens,
    ms, note: r.isAnswered ? why : r.reason,
  })
  if (ok) await update($, warm, () => ({ at: now, pings: (w?.pings ?? 0) + 1 }))
  else $.ui.toast(`cache-clock: keep-alive missed the cache (${r.isAnswered ? `read ${k(u?.cache_read_input_tokens ?? 0)} of ${k(ctx)}` : r.reason})`)
}

async function compactNow($: EngineInterface, ctx: number) {
  const now = await $.clock.now()
  try {
    const r = await $.session.compact()
    if (r.skip !== undefined) {
      await record($, { at: now, kind: 'compact', ctx, ok: false, note: r.skip })
      $.ui.toast(`cache-clock: auto-compact skipped — ${r.skip}`)
      return
    }
    const u = r.usage
    await record($, {
      at: now, kind: 'compact', ctx, ok: true, after: r.tokensAfter,
      read: u?.cache_read_input_tokens, written: u?.cache_creation_input_tokens, input: u?.input_tokens, output: u?.output_tokens,
    })
    await update($, compacted, () => ({ before: r.tokensBefore ?? ctx, after: r.tokensAfter ?? 0, isAuto: true }))
    await update($, last, () => null)
    await update($, warm, () => null)
    $.ui.toast(`Auto-compacted ${k(r.tokensBefore ?? ctx)}→${k(r.tokensAfter ?? 0)} before the prompt cache lapsed`)
  } catch (err) {
    // Rejects while a turn runs: the person came back first, nothing to do.
    $.ui.log(`cache-clock: auto-compact not run: ${String(err)}`, { to: 'debug' })
  }
}

async function act($: EngineInterface, c: Clock, touch: CacheTouch, w: Warmth | null, m: Mode) {
  const ctx = (await $.session.usage()).context.tokens ?? touch.ctx
  const pings = w?.pings ?? 0
  // pings only: never summarise the conversation, whatever its size.
  const planned = decide(ctx, pings, m === 'pings' ? { ...c, keepAliveBelow: Infinity, compactAbove: Infinity } : c)
  // Mid-turn the compaction is refused: ping instead, counting toward the same keep-alive cap, so a turn
  // stuck on an unanswered permission prompt does not keep the cache warm forever.
  const action = planned === 'compact' && c.turns.size > 0 ? (pings < c.maxKeepAlives ? 'ping' : 'lapse') : planned
  c.acting = true
  try {
    if (action === 'ping') {
      $.ui.status(planned === 'compact' ? 'cache ◌ keep-alive ping (turn still running, cannot compact)…' : 'cache ◌ keep-alive ping…')
      await ping($, ctx, w, planned === 'compact' ? 'turn running: compaction refused, pinged instead' : undefined)
    } else if (action === 'compact') {
      $.ui.status('cache ◌ auto-compacting before the cache lapses…')
      await compactNow($, ctx)
    } else {
      await record($, { at: await $.clock.now(), kind: 'lapse', ctx, ok: true, note: `${pings} keep-alives used${planned === 'compact' ? ', turn still running' : ''}` })
    }
  } catch (err) {
    $.ui.log(`cache-clock: ${action} failed: ${String(err)}`, { to: 'debug' })
  } finally {
    c.acting = false
  }
}

async function paint($: EngineInterface, c: Clock) {
  if (c.acting || !c.isLive) return
  const [touch, w, done, now, chosen] = await Promise.all([read($, last), read($, warm), read($, compacted), $.clock.now(), read($, mode)])
  const m = modeOf(c, chosen)
  const isBusy = c.busy > 0
  const expiresAt = touch ? (w?.at ?? touch.at) + c.ttlMs : 0
  const left = expiresAt - now
  const tokens = touch && !isBusy && left <= 0 ? (await $.session.usage()).context.tokens : undefined
  $.ui.status(line(touch, w, done, now, c.ttlMs, isBusy, tokens, m))

  // Act once per deadline, only while the entry is still warm. A laptop that
  // slept through the deadline wakes to an expired cache: nothing left to save,
  // so the stretch is only logged, to show how often sleep costs a re-write.
  if (touch && !isBusy && left <= 0 && m !== 'off' && c.actedFor !== expiresAt) {
    c.actedFor = expiresAt
    await record($, { at: now, kind: 'missed', ctx: touch.ctx, ok: false, note: `deadline passed ${fmt(-left)} ago unattended (asleep?)` })
    return
  }
  if (!touch || isBusy || left <= 0 || left > c.leadMs || c.actedFor === expiresAt) return
  c.actedFor = expiresAt
  if (m !== 'off') {
    await act($, c, touch, w, m)
    await paint($, c)
  } else {
    $.ui.toast(`Prompt cache lapses in ${fmt(left)} — send something to keep it warm`)
  }
}

const setTtl = (c: Clock, ttl: Ttl, source: string) => {
  // a resumed session's lapse was marked as handled under the old TTL: move the mark with it
  if (c.resumedAt !== undefined && c.actedFor === c.resumedAt + c.ttlMs) c.actedFor = c.resumedAt + TTL_MS[ttl]
  c.ttl = ttl
  c.ttlSource = source
  c.ttlMs = TTL_MS[ttl]
  c.leadMs = leadMsFor(c, ttl)
}

// Each TTL has its own lead: three minutes is a small slice of an hour, but more than half of five minutes, where
// acting at 2:30 pings or compacts for people who were about to come back anyway (30 s still lets a request land).
const leadMsFor = (c: Pick<Clock, 'leadMinutes' | 'leadMinutes5m'>, ttl: Ttl) =>
  Math.min((ttl === '5m' ? c.leadMinutes5m : c.leadMinutes) * 60 * 1000, TTL_MS[ttl] / 2)

const isTtl = (v: unknown): v is Ttl => v === '5m' || v === '1h'
const truthy = (v: unknown) => v !== undefined && v !== null && v !== '' && v !== '0' && v !== 'false' && v !== false

// Claude Code's own choice for the main conversation (CLI 2.1.293): FORCE_PROMPT_CACHING_5M, then
// CLAUDE_CODE_PROMPT_CACHE_TTL, then the promptCacheTtl setting, then ENABLE_PROMPT_CACHING_1H (and, on Bedrock,
// ENABLE_PROMPT_CACHING_1H_BEDROCK). Unset, it is 1 hour on a subscription within its limits and 5 minutes on an
// API key, Bedrock, Vertex or Foundry. Bedrock, Vertex and Foundry announce themselves in the environment; an API
// key in use does not (one may be set and unused), so that case is left to the cache itself (watchTtl).
export const ttlFromSettings = (s: Record<string, unknown>, env: Record<string, unknown>): [Ttl, string] | undefined => {
  const senv = (s.env ?? {}) as Record<string, unknown>
  const v = (name: string) => env[name] ?? senv[name]
  if (truthy(v('FORCE_PROMPT_CACHING_5M'))) return ['5m', 'FORCE_PROMPT_CACHING_5M']
  const fixed = v('CLAUDE_CODE_PROMPT_CACHE_TTL')
  if (isTtl(fixed)) return [fixed, 'CLAUDE_CODE_PROMPT_CACHE_TTL']
  if (isTtl(s.promptCacheTtl)) return [s.promptCacheTtl, 'promptCacheTtl setting']
  const bedrock = truthy(v('CLAUDE_CODE_USE_BEDROCK'))
  if (truthy(v('ENABLE_PROMPT_CACHING_1H')) || (bedrock && truthy(v('ENABLE_PROMPT_CACHING_1H_BEDROCK')))) return ['1h', 'ENABLE_PROMPT_CACHING_1H']
  if (bedrock) return ['5m', 'Bedrock default']
  if (truthy(v('CLAUDE_CODE_USE_VERTEX'))) return ['5m', 'Vertex default']
  if (truthy(v('CLAUDE_CODE_USE_FOUNDRY'))) return ['5m', 'Foundry default']
  return undefined
}

// The engine hands a mod only the four token counters, not the 5m/1h split, so the TTL is read off the cache's
// behaviour: a return 5–55 minutes after the last refresh either finds the context (an hour) or does not (five
// minutes). A miss can also come from a changed tool list, so it takes two misses in a row to conclude 5m; one
// hit is enough for 1h. Returns the new verdict, or undefined when this request says nothing.
export const watchTtl = (since: number, prevCtx: number, read: number, misses: number): { ttl?: Ttl; misses: number } => {
  if (prevCtx < 20_000 || since < 5.5 * 60 * 1000 || since > 55 * 60 * 1000) return { misses }
  if (read >= 0.8 * prevCtx) return { ttl: '1h', misses: 0 }
  if (read < 0.5 * prevCtx) return misses + 1 >= 2 ? { ttl: '5m', misses: misses + 1 } : { misses: misses + 1 }
  return { misses }
}

const modeOf = (c: Clock, chosen: Mode | null): Mode => chosen ?? (c.autoAct ? 'auto' : 'off')

const MODE_TEXT: Record<Mode, string> = {
  auto: 'on: pings and compactions follow the policy below',
  pings: 'pings only: keeps the cache warm, never compacts, so the conversation is never summarised',
  off: 'off: no pings, no compactions; a warning before the cache lapses',
}

// The slowest ping says how much of the lead a request needs: a ping that took most of it, or missed, means act earlier.
const pingTimes = (pings: LedgerEntry[]) => {
  const ms = pings.map(e => e.ms).filter((x): x is number => x !== undefined).sort((a, b) => a - b)
  return ms.length ? [`  ping round trip: median ${(ms[Math.floor(ms.length / 2)]! / 1000).toFixed(1)} s, slowest ${(ms[ms.length - 1]! / 1000).toFixed(1)} s`] : []
}

export const summarize = (entries: LedgerEntry[], p: Policy & { leadMinutes: number; leadMs?: number; ttl: string; ttlSource?: string }, m: Mode = 'auto') => {
  const pings = entries.filter(e => e.kind === 'ping')
  const compacts = entries.filter(e => e.kind === 'compact' && e.ok)
  const lines = [
    `This session: ${MODE_TEXT[m]}. Change it with /cache-clock on | pings | off.`,
    '',
    `Policy (TTL ${p.ttl}${p.ttlSource ? ` — ${p.ttlSource}` : ''}, acting ${p.leadMs !== undefined ? p.leadMs / 60000 : p.leadMinutes} min before expiry):`,
    `  context < ${k(p.keepAliveBelow)}: up to ${p.maxKeepAlives} keep-alive pings, then let it lapse`,
    `  ${k(p.keepAliveBelow)}–${k(p.compactAbove)}: 1 keep-alive ping, then compact at the next deadline`,
    `  ≥ ${k(p.compactAbove)}: compact at the first deadline`,
    '',
    `Keep-alive pings: ${pings.length} (${pings.filter(e => e.ok).length} hit the cache), ${k(pings.reduce((s, e) => s + (e.read ?? 0), 0))} read in total`,
    ...pingTimes(pings),
    `Deadlines slept through (no action possible): ${entries.filter(e => e.kind === 'missed').length}`,
    `Auto-compactions: ${compacts.length}` + (compacts.length ? `, ${k(compacts.reduce((s, e) => s + e.ctx, 0))} → ${k(compacts.reduce((s, e) => s + (e.after ?? 0), 0))}, compaction read ${k(compacts.reduce((s, e) => s + (e.read ?? 0), 0))} cached / ${k(compacts.reduce((s, e) => s + (e.input ?? 0), 0))} uncached` : ''),
  ]
  const recent = entries.slice(-8).reverse()
  if (recent.length) {
    lines.push('', 'Recent:')
    for (const e of recent) {
      const when = new Date(e.at).toISOString().slice(5, 16).replace('T', ' ')
      const what = e.kind === 'ping'
        ? `ping   ${k(e.ctx)} ctx · read ${k(e.read ?? 0)}${e.ok ? '' : ' MISSED'}`
        : e.kind === 'compact'
          ? `compact ${k(e.ctx)}→${e.after !== undefined ? k(e.after) : '?'}${e.ok ? '' : ` skipped: ${e.note}`}`
          : e.kind === 'missed'
            ? `missed  ${k(e.ctx)} ctx — ${e.note}`
            : `lapse  ${k(e.ctx)} ctx (${e.note})`
      lines.push(`  ${when}Z  ${what}`)
    }
  }
  return lines.join('\n')
}

export const register: Register = (on, options) => {
  const fixedTtl = isTtl(options.ttl) ? options.ttl : undefined     // 'auto' (the default) detects it
  const ttl: Ttl = fixedTtl ?? '1h'
  const ttlMs = TTL_MS[ttl]
  const leadMinutes = Number(options.leadMinutes ?? 3)
  const leadMinutes5m = Number(options.leadMinutes5m ?? 0.5)
  const clock: Clock = {
    ttl,
    ttlSource: fixedTtl ? 'cache-clock ttl setting' : 'assumed (subscription default) until a return shows otherwise',
    ttlFixed: fixedTtl !== undefined,
    misses: 0,
    ttlMs,
    leadMinutes,
    leadMinutes5m,
    leadMs: leadMsFor({ leadMinutes, leadMinutes5m }, ttl),
    autoAct: options.autoAct !== false,
    keepAliveBelow: Number(options.keepAliveBelowTokens ?? 125000),
    maxKeepAlives: Number(options.maxKeepAlives ?? 3),
    compactAbove: Number(options.compactAboveTokens ?? 300000),
    isLive: false,
    busy: 0,
    acting: false,
    actedFor: 0,
    turns: new Set<string>(),
  }

  on('session.start', async ($, e, next) => {
    const r = await next(e)
    // A -p run or an SDK host has nobody to walk away: no clock, no pings, no compactions.
    if (!e.isInteractive) return r
    clock.isLive = true
    if (!fixedTtl) {
      try {
        const env: Record<string, unknown> = {
          FORCE_PROMPT_CACHING_5M: await $.env.get('FORCE_PROMPT_CACHING_5M'),
          CLAUDE_CODE_PROMPT_CACHE_TTL: await $.env.get('CLAUDE_CODE_PROMPT_CACHE_TTL'),
          ENABLE_PROMPT_CACHING_1H: await $.env.get('ENABLE_PROMPT_CACHING_1H'),
          ENABLE_PROMPT_CACHING_1H_BEDROCK: await $.env.get('ENABLE_PROMPT_CACHING_1H_BEDROCK'),
          CLAUDE_CODE_USE_BEDROCK: await $.env.get('CLAUDE_CODE_USE_BEDROCK'),
          CLAUDE_CODE_USE_VERTEX: await $.env.get('CLAUDE_CODE_USE_VERTEX'),
          CLAUDE_CODE_USE_FOUNDRY: await $.env.get('CLAUDE_CODE_USE_FOUNDRY'),
        }
        const found = ttlFromSettings((await $.settings.read()) as Record<string, unknown>, env)
        if (found) {
          setTtl(clock, ...found)
          clock.ttlFixed = found[1] !== 'Bedrock default' && found[1] !== 'Vertex default' && found[1] !== 'Foundry default'
        }
      } catch (err) {
        $.ui.log(`cache-clock: settings not readable, TTL ${clock.ttl} assumed: ${String(err)}`, { to: 'debug' })
      }
    }
    await $.command.register({ name: 'cache-clock', argumentHint: '[on|pings|off]', immediate: true,
      description: 'Show what cache-clock has done, or turn it on / pings-only / off for this session' })
    $.clock.every(1000, () => void paint($, clock))
    await paint($, clock)
    return r
  })

  on('command.run', { command: 'cache-clock' }, async ($, e) => {
    const arg = e.args.trim().toLowerCase()
    const pick: Mode | undefined = arg === 'off' ? 'off' : arg === 'pings' ? 'pings' : arg === 'on' || arg === 'auto' ? 'auto' : undefined
    if (pick) {
      await update($, mode, () => pick)
      await paint($, clock)
      return { text: `cache-clock ${MODE_TEXT[pick]} — this session only.` }
    }
    if (arg) return { text: `Unknown option "${arg}". Use /cache-clock on, /cache-clock pings or /cache-clock off.` }
    const entries = ((await $.store.get(LEDGER)) as LedgerEntry[] | undefined) ?? []
    return { text: summarize(entries, clock, modeOf(clock, await read($, mode))) }
  })

  // A resumed transcript says how long it has been since its last response.
  on('classic.SessionStart', async ($, e, next) => {
    if (e.seconds_since_last_response !== undefined) {
      const now = await $.clock.now()
      const at = now - e.seconds_since_last_response * 1000
      await update($, last, () => ({ at, ctx: e.context_tokens ?? 0, read: 0, written: 0 }))
      clock.actedFor = at + clock.ttlMs // a lapse while the session was closed is not a missed action
      clock.resumedAt = at
      await update($, warm, () => null)
    }
    return next(e)
  }).catch(($, e, next) => next(e))

  // A turn counts as running once the main thread has made a model request in it (a subagent's or
  // teammate's loop carries an agentId and is skipped), until that turn completes.
  on('turn.complete', async ($, e, next) => {
    clock.turns.delete(e.turnId)
    return next(e)
  })

  on('turn.step', async function* ($, e, next) {
    if (e.agentId !== undefined || clock.acting) return yield* next(e)
    clock.turns.add(e.turnId)
    clock.busy += 1
    try {
      const r = yield* next(e)
      if (r.usage) {
        const u = r.usage
        const at = await $.clock.now()
        if (!clock.ttlFixed) {
          const [prev, w] = await Promise.all([read($, last), read($, warm)])
          if (prev) {
            const seen = watchTtl(at - Math.max(prev.at, w?.at ?? 0), prev.ctx, u.cache_read_input_tokens, clock.misses)
            clock.misses = seen.misses
            if (seen.ttl) setTtl(clock, seen.ttl, seen.ttl === '1h' ? 'observed: a return within the hour found the cache'
              : 'observed: two returns after 5 minutes missed the cache')
          }
        }
        await update($, last, () => ({ at, ctx: ctxOf(u), read: u.cache_read_input_tokens, written: u.cache_creation_input_tokens }))
        await update($, warm, () => null)
        await update($, compacted, () => null)
      }
      return r
    } finally {
      clock.busy -= 1
      void paint($, clock)
    }
  })

  // Any compaction (/compact, the engine's own, ours) replaces the cached prefix.
  on('session.compact', async ($, e, next) => {
    const r = await next(e)
    if (e.agentId === undefined && e.trigger !== 'precompute' && r.skip === undefined) {
      await update($, last, () => null)
      await update($, warm, () => null)
      if (e.trigger !== 'plugin') {
        await update($, compacted, () => ({ before: r.tokensBefore ?? 0, after: r.tokensAfter ?? 0, isAuto: false }))
      }
    }
    return r
  }).catch(($, e, next) => next(e))

  // /model forfeits the cache; /clear starts a new prefix.
  on('classic.PostModelSwitch', async ($, e, next) => {
    await update($, last, () => null)
    await update($, warm, () => null)
    return next(e)
  }).catch(($, e, next) => next(e))
  on('session.end', async ($, e, next) => {
    if (e.reason === 'clear') {
      await update($, last, () => null)
      await update($, warm, () => null)
      await update($, compacted, () => null)
    }
    return next(e)
  })
}
