// Chat page helpers

export function formatFileSize(bytes: number): string {
  if (bytes < 1024) return bytes + ' B'
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB'
  return (bytes / (1024 * 1024)).toFixed(1) + ' MB'
}

export { getWsBaseUrl } from '../../utils/ws'

// 模糊匹配得分：返回 0 表示不沾边（应被过滤掉），>0 越大越相关。
// 子串命中越靠前、目标越短，分越高；否则按词级命中数计分。
export function fuzzyScore(query: string, target: string): number {
  const q = (query || '').trim().toLowerCase()
  const t = (target || '').toLowerCase()
  if (!q) return 0
  if (!t) return 0
  const at = t.indexOf(q)
  if (at >= 0) {
    return 1000 - Math.min(at, 999) + Math.round((q.length / t.length) * 100)
  }
  const tokens = q.split(/\s+/).filter(Boolean)
  let hit = 0
  for (const tk of tokens) if (t.includes(tk)) hit += 1
  return hit ? hit * 50 : 0
}

// 把目标文本按匹配子串切成段，供前端高亮渲染（首匹配）。
export function highlightMatch(query: string, target: string): { text: string; hit: boolean }[] {
  const q = (query || '').trim().toLowerCase()
  const t = target || ''
  if (!q) return [{ text: t, hit: false }]
  const at = t.toLowerCase().indexOf(q)
  if (at < 0) return [{ text: t, hit: false }]
  return [
    { text: t.slice(0, at), hit: false },
    { text: t.slice(at, at + q.length), hit: true },
    { text: t.slice(at + q.length), hit: false },
  ].filter((s) => s.text.length > 0)
}
