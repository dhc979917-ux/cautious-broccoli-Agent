import type { TokenUsage } from '../pages/Chat/types'

export function formatTokenCount(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return '—'
  if (value > 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`
  if (value >= 10_000) return `${(value / 1_000).toFixed(1)}k`
  return Math.trunc(value).toLocaleString('en-US')
}

export function getUncachedInputTokens(usage: TokenUsage | null | undefined): number | null {
  if (!usage || usage.cache_input_tokens == null || !Number.isFinite(usage.input_tokens)
      || !Number.isFinite(usage.cache_input_tokens) || usage.cache_input_tokens < 0
      || usage.cache_input_tokens > usage.input_tokens) return null
  return usage.input_tokens - usage.cache_input_tokens
}

export function getUncachedTokenTotal(usage: TokenUsage | null | undefined): number | null {
  const uncachedInput = getUncachedInputTokens(usage)
  if (!usage || uncachedInput === null || !Number.isFinite(usage.output_tokens)) return null
  return uncachedInput + usage.output_tokens
}

export function summarizeTokenUsage(
  entries: Iterable<{ id?: number; run_id?: string; token_usage?: TokenUsage | null }>,
): TokenUsage | null {
  const runs = new Map<string, TokenUsage>()
  let index = 0
  for (const entry of entries) {
    const usage = entry.token_usage
    const key = entry.run_id || (entry.id != null ? `message:${entry.id}` : `pending:${index}`)
    index++
    if (!usage || !Number.isFinite(usage.input_tokens) || !Number.isFinite(usage.output_tokens)) continue
    const previous = runs.get(key)
    if (!previous || usage.input_tokens + usage.output_tokens >= previous.input_tokens + previous.output_tokens) {
      runs.set(key, usage)
    }
  }
  if (runs.size === 0) return null
  let input = 0
  let output = 0
  let cache = 0
  let cacheKnown = true
  for (const usage of runs.values()) {
    input += usage.input_tokens
    output += usage.output_tokens
    if (usage.cache_input_tokens == null || !Number.isFinite(usage.cache_input_tokens)) {
      cacheKnown = false
    } else {
      cache += usage.cache_input_tokens
    }
  }
  return {
    input_tokens: input,
    output_tokens: output,
    cache_input_tokens: cacheKnown ? cache : undefined,
    cache_rate: cacheKnown ? (input > 0 ? Math.round(cache / input * 1000) / 10 : 0) : undefined,
  }
}
