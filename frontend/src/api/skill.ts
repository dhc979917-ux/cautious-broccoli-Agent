import request from './request'

export interface SkillInfo {
  source_group?: 'system' | 'mine' | 'shared'
  resource_id?: string
  origin?: string
  version_id?: string
  id: number
  name: string
  display_name: string
  description: string | null
  // 中文展示信息（后端自动生成，可能为空；展示时优先于原文）
  display_name_zh?: string | null
  description_zh?: string | null
  // 所属技能包（可能为空；有值时前端按包分组展示）
  package_id?: number | null
  package_name?: string
  package_display_name?: string
  package_description?: string | null
  required_role: string
  is_active: number
  // 广场/聊天可见性（管理员态）：true=对未授权非管理员隐藏（仅管理员与被授权用户可见）
  is_hidden?: boolean
  // 磁盘身份与目录缺失标记：dir_missing=true 表示库里有记录但磁盘目录不在（管理端显示"目录缺失"）
  rel_path?: string | null
  dir_missing?: boolean
  created_at: string
  has_access: boolean
  uploaded_by?: string | null
  package_uploaded_by?: string | null
}

// 获取所有Skill列表（带权限标记）
export async function getAllSkills(): Promise<SkillInfo[]> {
  const res = await request.get('/skills/')
  return res.data
}

// ==================== 技能包上传（异步作业） ====================
// 后端受理后立即返回 job_id，落盘/同步/中文名生成在后台推进；前端轮询作业状态，
// 因此不会再出现"前端超时报失败、后端其实已经建好包"的错位（历史故障：nginx 499）。

export type UploadJobStatus = 'pending' | 'extracting' | 'syncing' | 'done' | 'failed'

export interface UploadJobInfo {
  id: number
  package_name: string
  kind: 'folder' | 'zip'
  status: UploadJobStatus
  message?: string | null
  error?: string | null
  file_count: number
  total_bytes: number
  new_skills: string[]
  stats?: Record<string, unknown> | null
  created_at?: string
  started_at?: string | null
  finished_at?: string | null
}

export interface UploadAccepted {
  job_id: number
  package_name: string
  status: UploadJobStatus
  message: string
}

// 上传体积可达 200MB：单独放宽超时，与 nginx proxy_read_timeout 300s 对齐
const UPLOAD_TIMEOUT = 300000

// 管理员上传技能包：文件夹传 files+相对路径；zip 传单个文件由服务端解压
export async function uploadSkillPackage(files: File[], paths: string[]): Promise<UploadAccepted> {
  const fd = new FormData()
  files.forEach((f) => fd.append('files', f))
  paths.forEach((p) => fd.append('paths', p))
  const res = await request.post('/skills/upload-package', fd, { timeout: UPLOAD_TIMEOUT })
  return res.data
}

export async function getUploadJob(jobId: number): Promise<UploadJobInfo> {
  const res = await request.get(`/skills/upload-jobs/${jobId}`)
  return res.data
}

export async function uploadSkillUpdate(skillId: number, file: File): Promise<{ version_id: string; message: string }> {
  const form = new FormData()
  form.append('file', file)
  return (await request.post(`/admin/skills/${skillId}/upload-update`, form, { timeout: UPLOAD_TIMEOUT })).data
}

export async function listUploadJobs(limit = 10): Promise<UploadJobInfo[]> {
  const res = await request.get('/skills/upload-jobs', { params: { limit } })
  return res.data
}

export const isUploadJobRunning = (job?: { status?: string } | null): boolean =>
  !!job && job.status !== 'done' && job.status !== 'failed'

export const UPLOAD_STAGE_LABELS: Record<UploadJobStatus, string> = {
  pending: '已受理，排队中',
  extracting: '正在解压落盘',
  syncing: '正在同步注册技能',
  done: '已完成',
  failed: '失败',
}

// 轮询作业直到终态：默认 1.5s 一次、最长 5 分钟；超时抛错（服务端可能仍在跑，稍后刷新列表确认）
export async function pollUploadJob(
  jobId: number,
  opts?: { intervalMs?: number; timeoutMs?: number; onProgress?: (job: UploadJobInfo) => void },
): Promise<UploadJobInfo> {
  const interval = opts?.intervalMs ?? 1500
  const deadline = Date.now() + (opts?.timeoutMs ?? 300000)
  for (;;) {
    const job = await getUploadJob(jobId)
    opts?.onProgress?.(job)
    if (!isUploadJobRunning(job)) return job
    if (Date.now() > deadline) throw new Error('后台处理超时，请稍后在技能列表确认结果')
    await new Promise((r) => setTimeout(r, interval))
  }
}
