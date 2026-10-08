export type CacheTouch = { at: number; ctx: number; read: number; written: number }
export type Compaction = { before: number; after: number; isAuto: boolean }
export type Warmth = { at: number; pings: number }
export type LedgerEntry = {
  at: number
  kind: 'ping' | 'compact' | 'lapse' | 'missed'
  ctx: number
  ok: boolean
  read?: number
  written?: number
  input?: number
  output?: number
  after?: number
  note?: string
}

declare module 'claude-code' {
  interface PluginState {
    'cache-clock': { last: CacheTouch | null; compacted: Compaction | null; warm: Warmth | null }
  }
}
