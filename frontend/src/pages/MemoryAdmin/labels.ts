export const SOURCE_LABELS: Record<string, string> = {
  legacy_unknown: '历史记忆', manual: '人工萃取', janitor: '定期摘要', auto_session: '会话记忆', auto_note: '自动记录',
  lifecycle_disable: '停用申请', lifecycle_delete: '删除申请', lifecycle_restore: '恢复申请',
}
export const STATUS_LABELS: Record<string, string> = {
  published: '已发布', pending: '待审核', accepted: '已接受', rejected: '已驳回', keep_both: '保留冲突内容',
  review: '待审核', queued: '排队中', generating: '萃取中', publishing: '发布中', index_pending: '等待索引',
  completed: '已完成', failed: '失败', expired: '已过期', disabled: '已停用', deleted: '已删除', superseded: '已替代',
  disabling: '停用中', deleting: '删除中', ready: '已就绪', removed: '已移除', legacy_unknown: '待同步',
}
export const METHOD_LABELS: Record<string, string> = { summary: '重点摘要', dedupe: '合并去重', timeline: '时间线整理', conflicts: '冲突梳理', structured: '逐条事实萃取' }
export const sourceLabel = (value?: string) => value ? SOURCE_LABELS[value] || '其他来源' : '历史记忆'
export const statusLabel = (value: string) => STATUS_LABELS[value] || '未知状态'
export const methodLabel = (value: string) => METHOD_LABELS[value] || '自定义萃取'
export const statusColor = (value: string) => ['published', 'completed', 'accepted', 'ready'].includes(value) ? 'success' : ['failed', 'rejected'].includes(value) ? 'error' : ['review', 'pending', 'queued', 'generating', 'index_pending'].includes(value) ? 'warning' : 'default'
export function memoryTitle(path?: string | null, position?: number): string {
  if (!path) return `来源 ${position === undefined ? '' : position + 1}`.trim()
  if (path.startsWith('digest/admin_review/')) return '审核后发布的记忆'
  const date = path.match(/\d{4}-\d{2}-\d{2}/)?.[0]
  if (path.startsWith('daily/')) return date ? `${date} · 会话记录` : '会话记录'
  if (path.startsWith('digest/')) return date ? `${date} · 摘要记忆` : '摘要记忆'
  return path.split('/').slice(-1)[0]?.replace(/\.md$/i, '') || '记忆内容'
}
export const taskFailure = (code: string | null) => code ? ({
  sensitive_source: '来源含敏感信息，无法萃取；请先检查来源。',
  unsupported_or_sensitive_item: '候选内容未通过安全或来源校验，请重新萃取。',
  missing_source_evidence: '候选缺少可靠的来源依据，请重新萃取。',
  invalid_model_output: '模型返回格式不符合要求，请重新萃取。',
} as Record<string, string>)[code] || '萃取未成功，可重新萃取；原始记忆不受影响。' : ''
export const dateLabel = (value?: string) => value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '—'
export const IN_PROGRESS = ['queued', 'generating', 'publishing', 'index_pending']
