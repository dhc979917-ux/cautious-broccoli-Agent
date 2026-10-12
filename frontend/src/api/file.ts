import request from './request'

export interface FileInfo {
  id: number
  user_id: number
  username?: string | null
  kind: 'upload' | 'output'
  original_name: string
  stored_name: string
  size: number
  content_type?: string | null
  session_id?: number | null
  agent_id?: number | null
  session_title?: string | null
  agent_name?: string | null
  // 来源会话属主（管理员判断能否预览他人会话）；会话已删除时为 null
  session_owner_id?: number | null
  // 来源会话是否已删除：true 时"来源"照常展示标题但置灰不可点
  session_deleted?: boolean
  created_at?: string | null
  deleted_at?: string | null
}

export interface MyUsage {
  used_bytes: number
  count: number
  quota_bytes: number
}

export interface GlobalStats {
  total_count: number
  total_bytes: number
  quota_mb_per_user: number
}

export interface FileStats {
  my_usage: MyUsage
  global?: GlobalStats
}

// 文件列表（普通用户只能看到自己的；管理员可用 user_id 过滤或查看全部）
export async function listFiles(params: {
  kind?: string
  q?: string
  deleted?: boolean
  user_id?: number | null
  // true=只看本人文件（管理员的"我的上传/分析产出"视图也走这个口径）
  mine?: boolean
}): Promise<FileInfo[]> {
  const res = await request.get('/files/', { params })
  return res.data
}

// 我的用量 + 配额；管理员额外返回全局统计
export async function fileStats(): Promise<FileStats> {
  const res = await request.get('/files/stats')
  return res.data
}

export async function renameFile(fileId: number, original_name: string) {
  const res = await request.put(`/files/${fileId}`, { original_name })
  return res.data
}

export async function softDeleteFile(fileId: number) {
  const res = await request.delete(`/files/${fileId}`)
  return res.data
}

export async function restoreFile(fileId: number) {
  const res = await request.post(`/files/${fileId}/restore`)
  return res.data
}

// 彻底删除（密码二次确认）
export async function hardDeleteFile(fileId: number, username: string, password: string) {
  const res = await request.delete(`/files/${fileId}/hard`, {
    data: { username, password },
  })
  return res.data
}

// 管理员：磁盘对账
export async function syncFiles() {
  const res = await request.post('/files/sync')
  return res.data
}

// 批量下载（zip）：返回 blob
export async function batchDownload(fileIds: number[]): Promise<Blob> {
  const res = await request.post('/files/download-zip', { file_ids: fileIds }, { responseType: 'blob' })
  return res.data
}

// 单文件下载 URL（配合 fetch + Authorization 使用）
export function fileDownloadUrl(fileId: number): string {
  return `/api/v1/files/${fileId}/download`
}
