import request from './request'

// 监控与运维 API（非 admin 只能看自己；admin 可传 userId 看指定用户，不传看全局）

export interface MonitoringOverview {
  sessions: number
  messages: number
  input_tokens: number
  output_tokens: number
  total_tokens: number
  tool_calls: number
  avg_elapsed_seconds: number
  errors: number
  running_tasks: number
  // 质量闭环（先问再做 / 做完再看）指标；改造前的历史消息没有该字段，可选
  quality?: {
    artifact_replies: number
    intake_replies: number
    intake_rate: number
    intake_questions: number
    intake_assumed: number
    assume_rate: number
    verified_replies: number
    verify_rate: number
    verify_errors: number
    avg_verify_errors: number
    verify_tiers: Record<string, number>
  }
}

export interface TrendPoint {
  date: string
  key: string
  tokens: number
  messages: number
}

export interface DayConversation {
  session_id: number
  title: string
  agent_id: number | null
  agent_name: string
  messages: number
  tokens: number
  elapsed_seconds: number
  models: string[]
  first_ts: string | null
}

export interface ConversationMessageDetail {
  id: number
  role: string
  ts: string | null
  model?: string | null
  tokens: number
  elapsed_seconds?: number | null
  tool_calls?: number | null
  preview: string
}

export interface TaskInfo {
  session_id: number
  session_title: string
  status: string
  started_at: number
  finished_at: number | null
  elapsed_seconds: number | null
}

export interface UserOverview {
  user_id: number
  username: string
  role: string
  is_active: boolean
  sessions: number
  messages: number
  total_tokens: number
  week_tokens: number
  last_active: string | null
}

export async function getOverview(userId?: number, days?: number): Promise<MonitoringOverview> {
  const params: Record<string, number> = {}
  if (userId) params.user_id = userId
  if (days) params.days = days
  const res = await request.get('/monitoring/overview', { params })
  return res.data
}

export async function getTrend(days = 30, groupBy: 'model' | 'agent' = 'model', userId?: number): Promise<TrendPoint[]> {
  const res = await request.get('/monitoring/trend', {
    params: { days, group_by: groupBy, ...(userId ? { user_id: userId } : {}) },
  })
  return res.data
}

export async function getDayConversations(date: string, userId?: number): Promise<DayConversation[]> {
  const res = await request.get(`/monitoring/days/${date}/conversations`, {
    params: userId ? { user_id: userId } : {},
  })
  return res.data
}

export async function getConversationDetail(sessionId: number): Promise<ConversationMessageDetail[]> {
  const res = await request.get(`/monitoring/conversations/${sessionId}/detail`)
  return res.data
}

export async function getTasks(userId?: number): Promise<TaskInfo[]> {
  const res = await request.get('/monitoring/tasks', { params: userId ? { user_id: userId } : {} })
  return res.data
}

export async function getUsersOverview(): Promise<UserOverview[]> {
  const res = await request.get('/monitoring/users')
  return res.data
}
