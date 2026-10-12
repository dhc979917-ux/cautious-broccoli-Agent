import axios from 'axios'
import request from './request'

export type MemoryOwner = { id: number; account: string; username: string }
export type MemoryEntry = { id: string; path: string; source_type: string; status: string; index_status: string; revision: number; created_at: string; source_agent_id?: number | null; source_session_id?: number | null }
export type ExtractionMethod = { code: string; title: string; revision: number }
export type MemoryTask = { id: string; status: string; source_type: string; method_code: string; revision: number; error_code: string | null; created_at: string }
export type MemoryItem = { id: string; content: string; status: string; revision: number; conflict_group: string | null; source_ids: string[]; evidence: { source_id: string; quote: string }[] }
export type MemorySource = { id: string; content: string; sha256: string; path?: string | null; source_type?: string; session_id?: number | null; agent_id?: number | null }
export type MemoryAccess = { create: boolean; review: boolean; publish: boolean }
export type MemoryTaskDetail = MemoryTask & { user_id: number; sources: MemorySource[]; items: MemoryItem[]; content_removed?: boolean; review_access: { can_review: boolean; can_publish: boolean; requires_reason: boolean; reason_recorded?: boolean } }
export type ReviewDecision = 'accepted' | 'rejected' | 'keep_both'

export const listMemoryOwners = (search = '') => request.get<{ items: MemoryOwner[] }>('/admin/memories/users', { params: { search } }).then((response) => response.data.items)
export const getMemoryAccess = (userId: number) => request.get<MemoryAccess>('/admin/memories/access', { params: { user_id: userId } }).then((response) => response.data)
export const listMemories = (params: { user_id: number; page: number; keyword?: string; source_type?: string; status?: string }) => request.get<{ items: MemoryEntry[]; total: number }>('/admin/memories/entries', { params }).then((response) => response.data)
export const readMemory = (entryId: string, userId: number) => request.get<MemorySource & { path: string }>(`/admin/memories/entries/${entryId}`, { params: { user_id: userId } }).then((response) => response.data)
export const listMemoryTasks = (userId: number, page = 1, status?: string) => request.get<{ items: MemoryTask[]; total: number }>(
  '/admin/memories/tasks', { params: { user_id: userId, page, status } }).then((response) => response.data)
export const listExtractionMethods = (userId: number) => request.get<ExtractionMethod[]>('/admin/memories/methods', { params: { user_id: userId } }).then((response) => response.data)
export const readMemoryTask = (taskId: string) => request.get<MemoryTaskDetail>(`/admin/memories/tasks/${taskId}`).then((response) => response.data)
export function createMemoryExtraction(userId: number, entryIds: string[], methodCode: string) {
  const bytes = crypto.getRandomValues(new Uint8Array(16))
  const key = Array.from(bytes, (value) => value.toString(16).padStart(2, '0')).join('')
  return request.post<MemoryTask>('/admin/memories/tasks', { user_id: userId, entry_ids: entryIds, method_code: methodCode, idempotency_key: key }).then((response) => response.data)
}
export const reviewMemoryItem = (taskId: string, item: MemoryItem, decision: ReviewDecision, content?: string, reason?: string) => request.put(
  `/admin/memories/tasks/${taskId}/items/${item.id}`, { revision: item.revision, decision, ...(content !== undefined ? { content } : {}), ...(reason ? { reason } : {}) })
export const publishMemoryTask = (task: MemoryTask) => request.post<MemoryTask>(`/admin/memories/tasks/${task.id}/publish`, { revision: task.revision }).then((response) => response.data)
export const retryMemoryTask = (task: MemoryTask) => request.post(`/admin/memories/tasks/${task.id}/retry`, { revision: task.revision })
export const changeMemoryStatus = (entry: MemoryEntry, userId: number, action: 'disable' | 'restore') => request.post<MemoryTask>(
  `/admin/memories/entries/${entry.id}/lifecycle`, { user_id: userId, action, revision: entry.revision }).then((response) => response.data)
export function memoryError(cause: unknown): string {
  if (axios.isAxiosError(cause)) {
    const detail = cause.response?.data?.detail
    if (typeof detail === 'string') return detail
    if (Array.isArray(detail)) return '提交内容不符合要求，请检查输入后重试。'
    if (cause.response?.status === 403) return '您没有此用户记忆的操作权限。'
    if (cause.response?.status === 409) return '内容已被其他人处理，请刷新后重试。'
    if (!cause.response) return '网络连接失败，请稍后重试。'
  }
  return '操作未完成，请稍后重试。'
}
