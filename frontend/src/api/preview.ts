// 服务预览 API：查询/刷新/停止 Agent 在会话沙箱里起的服务
// 后端总开关 PREVIEW_ENABLED 关闭时 list 返回 { enabled: false, items: [] }
import request from './request'
import type { ServicePreview } from '../pages/Chat/types'

export interface PreviewListResponse {
  enabled: boolean
  items: ServicePreview[]
}

export async function listSessionPreviews(sessionId: number): Promise<PreviewListResponse> {
  const { data } = await request.get(`/previews/sessions/${sessionId}`)
  return data
}

// 重新探测沙箱：服务还活着就续期，已经没了就置为失效并带回原因
export async function refreshPreview(previewId: number): Promise<ServicePreview> {
  const { data } = await request.post(`/previews/${previewId}/refresh`)
  return data
}

// 停止预览：立刻摘掉代理容器，释放宿主端口
export async function stopPreview(previewId: number): Promise<ServicePreview> {
  const { data } = await request.post(`/previews/${previewId}/stop`)
  return data
}
