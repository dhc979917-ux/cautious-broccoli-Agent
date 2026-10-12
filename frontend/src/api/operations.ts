import request from './request'

export type Dashboard = {
  timezone: string; since: string; until: string; updated_at: string; note: string
  resources: Record<string, number>; pending: Record<string, number>
  usage: { agent_replies: number; active_users: number; model_calls: number; tool_calls: number; failure_rate: number | null; tokens: { input: number; output: number; complete_samples: number; missing_samples: number }; cost_note: string }
  samples: { truncated: boolean; source: string }
  alerts: { code: string; value: number | null; threshold: number; active: boolean; responsible_user_id: number | null; href: string; sample?: string }[]
  recent: AuditRow[]
}
export type AuditRow = { id: number; actor_id?: number; action: string; resource_type: string; resource_id: string | null; result: string; request_id: string; trace_id: string | null; created_at: string }
export type TraceRow = { trace_id: string; user_id: number | null; status: string; started_at: string; model_calls: number; tool_calls: number; metadata: Record<string, unknown> }
export type TraceDetail = TraceRow & { note: string; spans: { span_id: string; parent_span_id: string | null; kind: string; duration_ms: number; status: string; start_time: string; attributes: Record<string, unknown> }[] }
export type Policy = { resource: string; retention_days: number; backlog_threshold: number; responsible_user_id: number | null; revision: number }
export type Cleanup = { id: string; resource: string; status: string; revision: number; target_count: number; requested_by: number; reviewed_by: number | null }
export const dashboard = (params = {}) => request.get<Dashboard>('/admin/operations/dashboard', { params }).then((response) => response.data)
export const pending = () => request.get<{ items: { type: string; id: string; owner_id?: number; status: string; href: string }[]; note: string }>('/admin/operations/pending').then((response) => response.data)
export type GatewayTest = { id: number; model_id: string; actor_id: number; trace_id: string; outcome: string; error_code: string | null; duration_ms: number; tested_at: string }
export const gatewayTests = () => request.get<GatewayTest[]>('/admin/operations/gateway-tests').then((response) => response.data)
export const policies = () => request.get<Policy[]>('/admin/operations/policies').then((response) => response.data)
export const savePolicy = (row: Policy) => request.put(`/admin/operations/policies/${row.resource}`, row)
export const cleanupList = () => request.get<Cleanup[]>('/admin/operations/cleanup').then((response) => response.data)
export const requestCleanup = (data: { resource: string; cutoff: string; reason: string }) => request.post('/admin/operations/cleanup', data)
export const reviewCleanup = (id: string, data: { revision: number; decision: string; reason: string }) => request.post(`/admin/operations/cleanup/${id}/review`, data)
export const executeCleanup = (id: string) => request.post(`/admin/operations/cleanup/${id}/execute`)
export const audits = (params: object) => request.get<{ items: AuditRow[]; total: number; note: string }>('/admin/audit/events', { params }).then((response) => response.data)
export const traces = (params: object) => request.get<{ items: TraceRow[]; total: number; samples: { truncated: boolean } }>('/admin/operations/traces', { params }).then((response) => response.data)
export const traceDetail = (id: string, params: object) => request.get<TraceDetail>(`/admin/operations/traces/${id}`, { params }).then((response) => response.data)
export const labels: Record<string, string> = { users: '账号', agents: 'Agent', skills: 'Skill', models: '网关模型', mcp: 'MCP', gateway: '网关测试失败', tool_approvals: 'Tool待确认', tool_approval: 'Tool确认积压', memory_review: '记忆待审核', memory_failed: '记忆生成失败', memory_backlog: '记忆候选积压', index_pending: '索引待对账', index: '索引不一致', agent_drafts: 'Agent草稿', skill_drafts: 'Skill草稿', failure_rate: 'Trace失败率(%)', candidate_memory: '候选记忆', published_memory: '已发布记忆', trace: '原始Trace', audit: '管理审计' }
