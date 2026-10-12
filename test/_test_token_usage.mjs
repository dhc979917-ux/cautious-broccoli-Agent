import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

const frontend = resolve(import.meta.dirname, '../frontend')
const requireFrontend = createRequire(resolve(frontend, 'package.json'))
const ts = requireFrontend('typescript')
const source = readFileSync(resolve(frontend, 'src/utils/tokenUsage.ts'), 'utf8')
const output = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 } }).outputText
const module = { exports: {} }
new Function('module', 'exports', output)(module, module.exports)
const { formatTokenCount, getUncachedInputTokens, getUncachedTokenTotal, summarizeTokenUsage } = module.exports

for (const [value, expected] of [
  [0, '0'], [9999, '9,999'], [10000, '10.0k'], [1000000, '1000.0k'], [1000001, '1.0M'],
]) assert.equal(formatTokenCount(value), expected)
assert.equal(formatTokenCount(undefined), '—')
assert.equal(getUncachedInputTokens({ input_tokens: 410583, output_tokens: 14164, cache_input_tokens: 375808 }), 34775)
assert.equal(getUncachedTokenTotal({ input_tokens: 410583, output_tokens: 14164, cache_input_tokens: 375808 }), 48939)
assert.equal(getUncachedInputTokens({ input_tokens: 100, output_tokens: 2 }), null)
assert.equal(getUncachedInputTokens({ input_tokens: 10, output_tokens: 2, cache_input_tokens: 11 }), null)
assert.equal(getUncachedInputTokens({ input_tokens: 10, output_tokens: 2, cache_input_tokens: 0 }), 10)
assert.equal(getUncachedTokenTotal({ input_tokens: 10, output_tokens: 2 }), null)

const tooltip = readFileSync(resolve(frontend, 'src/components/TokenUsageTooltip/index.tsx'), 'utf8')
assert.match(tooltip, /输入（未命中）：.*getUncachedInputTokens\(usage\)/)
assert.match(tooltip, /输出：.*usage\?\.output_tokens/)
assert.match(tooltip, /缓存命中输入：.*usage\?\.cache_input_tokens/)
assert.equal((tooltip.match(/<div><span>/g) || []).length, 3)

const first = { input_tokens: 100, output_tokens: 20, cache_input_tokens: 0 }
const latest = { input_tokens: 300, output_tokens: 40, cache_input_tokens: 150 }
const messages = [
  { id: 1, run_id: 'run-1', token_usage: first },
  { id: 2, run_id: 'run-1', token_usage: latest },
  { run_id: 'run-1', token_usage: latest },
  { id: 3, token_usage: { input_tokens: 100, output_tokens: 5, cache_input_tokens: 0 } },
]
assert.deepEqual(summarizeTokenUsage(messages), {
  input_tokens: 400, output_tokens: 45, cache_input_tokens: 150, cache_rate: 37.5,
})
assert.equal(summarizeTokenUsage([{ id: 4, token_usage: first }]).cache_rate, 0)
assert.equal(summarizeTokenUsage([{ id: 5, token_usage: { input_tokens: 4, output_tokens: 2 } }]).cache_input_tokens, undefined)
assert.equal(summarizeTokenUsage([]), null)
console.log('Token format, three tooltip metrics, cumulative usage, replay deduplication and missing metrics: PASS')
