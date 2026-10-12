// 调度任务 API（后端 /api/v1/scheduled-jobs）
// 权限模型：管理员新建公共任务（is_public=true）或个人任务（is_public=false，仅自己可见）；
// 普通用户选择公共任务生成个人实例（传 parent_id，后端强制 is_public=false）。
import request from './request'

export type TriggerType = 'cron' | 'once'
export type JobStatus = 'running' | 'success' | 'failed' | 'skipped' | 'timeout'
export type NotifyOn = 'none' | 'failure' | 'always'

export interface ScheduledJob {
  id: number
  user_id: number
  owner_name: string | null
  parent_id: number | null      // null=公共任务；非 null=个人实例
  is_public: boolean            // true=公共任务（全员可见可选）；false=个人任务/用户实例
  deleted_at: string | null
  agent_id: number
  name: string
  description: string | null
  prompt: string
  trigger_type: TriggerType
  cron_expr: string | null
  run_at: string | null
  timezone: string
  schedule_desc: string
  model: string | null
  skill_versions?: Record<string, string>
  selected_skills: string[] | null
  selected_mcps: number[] | null
  enabled: boolean
  skip_if_running: boolean
  timeout_minutes: number
  max_retries: number
  retry_interval_minutes: number
  misfire_policy: 'run_once' | 'skip'
  notify_on: NotifyOn
  notify_channels: string[]
  last_run_at: string | null
  last_status: JobStatus | null
  next_run_at: string | null
  created_at: string
}

export interface JobRun {
  id: number
  job_id: number
  session_id: number | null
  // 该条执行记录归属的用户：公共任务的管理员聚合视图里用来标明是谁跑的，
  // 并据此决定"查看会话"是跳转（本人）还是只读预览（他人）
  owner_id: number | null
  owner_name: string | null
  trigger_type: 'cron' | 'once' | 'manual' | 'retry'
  attempt: number
  status: JobStatus
  error: string | null
  reply_summary: string | null
  started_at: string | null
  finished_at: string | null
}

export interface JobStats {
  enabled: number
  running: number
  today_total: number
  today_success: number
  next: { job_id: number; name: string; at: string } | null
}

// 管理员新建公共任务 / 用户选择公共任务生成个人实例的入参。
// agent_id 与执行策略由后端固化，前端不再提供。
export interface JobPayload {
  name?: string
  description?: string | null
  prompt?: string
  parent_id?: number | null
  is_public?: boolean           // 仅管理员新建时生效：默认 true（公共任务）
  trigger_type: TriggerType
  cron_expr?: string | null
  run_at?: string | null
  timezone?: string
  model?: string | null
  selected_skills?: string[] | null
  selected_mcps?: number[] | null
}

export interface SchedulePreview {
  description: string
  next_runs: string[]
}

// scope=deleted：回收站列表（管理员=全部；普通用户=仅本人任务/实例）
// scope: 不传=公共任务+本人任务；'all'=全部任务（仅管理员，含他人个人任务/实例）；
// 'deleted'=回收站（管理员为全量，普通用户仍只有公共+本人）
export async function listJobs(scope?: 'all' | 'deleted'): Promise<ScheduledJob[]> {
  const { data } = await request.get('/scheduled-jobs/', { params: scope ? { scope } : {} })
  return data
}

export async function getJobStats(): Promise<JobStats> {
  const { data } = await request.get('/scheduled-jobs/stats')
  return data
}

export async function previewSchedule(payload: {
  trigger_type: TriggerType
  cron_expr?: string | null
  run_at?: string | null
  timezone?: string
}): Promise<SchedulePreview> {
  const { data } = await request.post('/scheduled-jobs/preview', payload)
  return data
}

export async function createJob(payload: JobPayload): Promise<ScheduledJob> {
  const { data } = await request.post('/scheduled-jobs/', payload)
  return data
}

export async function updateJob(id: number, payload: Partial<JobPayload>): Promise<ScheduledJob> {
  const { data } = await request.put(`/scheduled-jobs/${id}`, payload)
  return data
}

export async function toggleJob(id: number): Promise<ScheduledJob> {
  const { data } = await request.patch(`/scheduled-jobs/${id}/toggle`)
  return data
}

// 统一软删除移入回收站：公共任务仅管理员；个人任务/实例由属主或管理员操作
export async function deleteJob(id: number): Promise<{ message: string }> {
  const { data } = await request.delete(`/scheduled-jobs/${id}`)
  return data
}

// 彻底删除：公共任务仅管理员（级联个人实例）；个人任务/实例由属主或管理员操作
export async function hardDeleteJob(id: number): Promise<{ message: string }> {
  const { data } = await request.delete(`/scheduled-jobs/${id}/hard`)
  return data
}

// 从回收站恢复：公共任务仅管理员；个人任务/实例由属主或管理员操作
export async function restoreJob(id: number): Promise<ScheduledJob> {
  const { data } = await request.post(`/scheduled-jobs/${id}/restore`)
  return data
}

export async function runJobNow(id: number): Promise<{ message: string }> {
  const { data } = await request.post(`/scheduled-jobs/${id}/run`)
  return data
}

// userId：仅管理员查看自己创建的公共任务时生效，按实例属主过滤聚合的执行历史
export async function listRuns(
  jobId: number, limit = 20, offset = 0, userId?: number,
): Promise<{ total: number; items: JobRun[] }> {
  const { data } = await request.get(`/scheduled-jobs/${jobId}/runs`, {
    params: { limit, offset, user_id: userId },
  })
  return data
}
