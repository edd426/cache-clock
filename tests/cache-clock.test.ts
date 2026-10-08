import { expect, mock, test } from 'claude-code/testing'
import type { On, SessionUsage, TurnStepResult } from 'claude-code'
import type { Engine } from 'claude-code/testing'

import { decide, summarize } from '../hooks/register'

const MIN = 60 * 1000
const policy = { keepAliveBelow: 125_000, maxKeepAlives: 3, compactAbove: 300_000 }

function harness(on: On, ctx: number, opts: { forkRead?: number } = {}) {
  const clock = mock.clock(on, { now: 1_000_000 })
  mock.store(on)
  const statuses: (string | undefined)[] = []
  const toasts: string[] = []
  const actions: string[] = []
  on('ui.status', ($, e) => { statuses.push(e.text); return { value: undefined } })
  on('ui.toast', ($, e) => { toasts.push(e.text); return { value: undefined } })
  on('ui.log', () => ({ value: undefined }))
  on('command.register', ($, e) => ({ value: { command: e.name } }))
  on('session.usage', () => ({ value: { startedAt: 0, context: { tokens: ctx, window: 1_000_000 }, rateLimits: [], version: 'test' } as SessionUsage }))
  on('session.start', ($, e) => ({ cwd: e.cwd }))
  on('turn.complete', () => ({ text: '' }))
  on('model.fork', () => {
    actions.push('ping')
    return { value: { isAnswered: true as const, text: 'ok', usage: { input_tokens: 20, output_tokens: 2, cache_read_input_tokens: opts.forkRead ?? ctx, cache_creation_input_tokens: 0 } } }
  })
  on('session.compact', () => {
    actions.push('compact')
    return { messages: [{ role: 'user' as const, text: 'summary', toolUses: [] }], tokensBefore: ctx, tokensAfter: 14_000, usage: { input_tokens: 1500, output_tokens: 4000, cache_read_input_tokens: ctx, cache_creation_input_tokens: 0 } }
  })
  on('turn.step', async function* ($, e): AsyncGenerator<never, TurnStepResult> {
    return { turnId: e.turnId, index: e.index, answer: 'ok', toolUses: [], stopReason: 'end_turn',
      usage: { input_tokens: 10, output_tokens: 0, cache_read_input_tokens: ctx - 10, cache_creation_input_tokens: 0, model: 'claude-opus-5-5' } }
  })
  return { clock, statuses, toasts, actions }
}

async function step($: Engine, agentId?: string, turnId = 't1') {
  const s = $.turn.step({ turnId, index: 0, model: 'claude-opus-5-5', messageCount: 3, agentId })
  for await (const _ of s) { /* drain */ }
  return s.result
}

// A finished turn: one main-thread step, then the turn completes.
async function turn($: Engine) {
  const r = await step($)
  await $.turn.complete({ turnId: 't1', reason: 'answer', answer: 'ok', durationMs: 1, isAborted: false } as Parameters<Engine['turn']['complete']>[0])
  return r
}
// origin and presentation are stamped by the engine in a session
const runCommand = ($: Engine) => $.command.run({ command: 'cache-clock', args: '' } as Parameters<Engine['command']['run']>[0])
const start = ($: Engine) => $.session.start({ cwd: '/x', surface: 'terminal', isInteractive: true })

test('decide: the three bands', () => {
  expect([0, 1, 2, 3].map(n => decide(80_000, n, policy))).toEqual(['ping', 'ping', 'ping', 'lapse'])
  expect([0, 1, 2].map(n => decide(200_000, n, policy))).toEqual(['ping', 'compact', 'compact'])
  expect(decide(300_000, 0, policy)).toBe('compact')
  expect(decide(124_999, 0, policy)).toBe('ping')
})

test('countdown only, acting 3 minutes before expiry', async ($, on) => {
  const h = harness(on, 80_000)
  await start($)
  await turn($)
  await h.clock.advance(10 * MIN)
  expect(h.statuses.at(-1)).toBe('cache ◷ 50m00s left')
  await h.clock.advance(46 * MIN)
  expect(h.actions).toEqual([])
  await h.clock.advance(1 * MIN + 1000)
  expect(h.actions).toEqual(['ping'])
})

test('small context: three keep-alives, then it lapses', async ($, on) => {
  const h = harness(on, 80_000)
  await start($)
  await turn($)
  await h.clock.advance(57 * MIN + 1000)
  expect(h.statuses.at(-1)).toMatch(/^cache ◷ 59m5\ds left · kept warm ×1$/)
  for (let i = 0; i < 6; i++) await h.clock.advance(30 * MIN)
  expect(h.actions).toEqual(['ping', 'ping', 'ping'])
  expect(h.statuses.at(-1)).toBe('cache ✕ expired · next prompt re-writes ~80k')
  const { text } = await runCommand($)
  expect(text).toContain('Keep-alive pings: 3 (3 hit the cache)')
  expect(text).toContain('lapse  80k ctx (3 keep-alives used)')
})

test('middle band: one keep-alive, then compact at the second deadline', { timeoutMs: 20_000 }, async ($, on) => {
  const h = harness(on, 200_000)
  await start($)
  await turn($)
  await h.clock.advance(58 * MIN)
  expect(h.actions).toEqual(['ping'])
  await h.clock.advance(60 * MIN)
  expect(h.actions).toEqual(['ping', 'compact'])
  expect(h.statuses.at(-1)).toBe('cache ○ auto-compacted 200k→14k · next prompt writes ~14k')
  for (let i = 0; i < 10; i++) await h.clock.advance(30 * MIN)
  expect(h.actions.length).toBe(2)
})

test('large context: compact at the first deadline', async ($, on) => {
  const h = harness(on, 450_000)
  await start($)
  await turn($)
  await h.clock.advance(58 * MIN)
  expect(h.actions).toEqual(['compact'])
  const { text } = await runCommand($)
  expect(text).toContain('Auto-compactions: 1, 450k → 14k, compaction read 450k cached / 2k uncached')
})

test('a -p run or SDK host: no clock, no actions', async ($, on) => {
  const h = harness(on, 450_000)
  await $.session.start({ cwd: '/x', surface: null, isInteractive: false })
  await turn($)
  await h.clock.advance(58 * MIN)
  expect(h.actions).toEqual([])
  expect(h.statuses).toEqual([])
})

test('a returning prompt resets the stretch', async ($, on) => {
  const h = harness(on, 80_000)
  await start($)
  await turn($)
  await h.clock.advance(58 * MIN)
  await turn($)
  await h.clock.advance(30 * MIN)
  expect(h.statuses.at(-1)).toBe('cache ◷ 30m00s left')
})

test('a ping that misses the cache is reported and does not extend the clock', async ($, on) => {
  const h = harness(on, 80_000, { forkRead: 3_000 })
  await start($)
  await turn($)
  await h.clock.advance(58 * MIN)
  expect(h.toasts).toEqual([expect.stringContaining('keep-alive missed the cache')])
  await h.clock.advance(3 * MIN)
  expect(h.statuses.at(-1)).toBe('cache ✕ expired · next prompt re-writes ~80k')
})

test('autoAct off only warns', { options: { autoAct: false } }, async ($, on) => {
  const h = harness(on, 450_000)
  await start($)
  await turn($)
  await h.clock.advance(58 * MIN)
  expect(h.actions).toEqual([])
  expect(h.toasts).toEqual([expect.stringContaining('Prompt cache lapses in')])
})

test('subagent requests do not reset the clock', { options: { ttl: '5m' } }, async ($, on) => {
  const h = harness(on, 80_000)
  await start($)
  await turn($)
  await h.clock.advance(60 * 1000)
  await step($, 'agent-1')
  await h.clock.advance(1000)
  expect(h.statuses.at(-1)).toBe('cache ◷ 3m59s left')
})

test('summary counts deadlines slept through', () => {
  const text = summarize([{ at: 0, kind: 'missed', ctx: 200_000, ok: false, note: 'deadline passed 3h00m ago unattended (asleep?)' }], { ...policy, leadMinutes: 3, ttl: '1h' })
  expect(text).toContain('Deadlines slept through (no action possible): 1')
  expect(text).toContain('missed  200k ctx — deadline passed 3h00m ago unattended (asleep?)')
})

test('a turn still running at the deadline gets a ping, not a refused compaction', async ($, on) => {
  const h = harness(on, 450_000)
  await start($)
  await step($)
  await h.clock.advance(58 * MIN)
  expect(h.actions).toEqual(['ping'])
  const { text } = await runCommand($)
  expect(text).toContain('Keep-alive pings: 1 (1 hit the cache)')
})

test('a stuck turn gets at most maxKeepAlives substitute pings, then the cache lapses', { timeoutMs: 20_000 }, async ($, on) => {
  const h = harness(on, 450_000)
  await start($)
  await step($)
  for (let i = 0; i < 10; i++) await h.clock.advance(30 * MIN)
  expect(h.actions).toEqual(['ping', 'ping', 'ping'])
  const { text } = await runCommand($)
  expect(text).toContain('3 keep-alives used, turn still running')
})

test('a subagent step does not mark the main thread as mid-turn', async ($, on) => {
  const h = harness(on, 450_000)
  await start($)
  await turn($)
  await step($, 'agent-1', 'sub-turn')
  await h.clock.advance(58 * MIN)
  expect(h.actions).toEqual(['compact'])
})
