// 会话只读预览 API（文件中心"来源"浮窗、调度任务执行历史共用）
// 消息结构与 /sessions/{id}/messages 完全一致，因此可直接复用聊天气泡组件渲染
import request from './request'
import type { ChatMessage } from '../pages/Chat/types'

export interface SessionPreviewMeta {
  id: number
  title: string
  agent_id: number | null
  agent_name: string | null
  user_id: number
  username: string | null
  created_at: string | null
  updated_at: string | null
  // 是否本人会话：决定浮窗里是否给"在聊天中打开"与下载入口
  is_own: boolean
}

export interface SessionPreview {
  session: SessionPreviewMeta
  messages: ChatMessage[]
  has_more: boolean
}

export async function getSessionPreview(sessionId: number, limit = 200): Promise<SessionPreview> {
  const { data } = await request.get(`/sessions/${sessionId}/preview`, { params: { limit } })
  return data
}

// 会话列表项（与后端 SessionResponse 一致）
export interface SessionListItem {
  id: number
  agent_id: number
  agent_name: string | null
  title: string
  status: string
  created_at: string
  updated_at: string | null
}

// 管理员查看指定用户的会话列表（需 monitor_manage 权限）
export async function getSessionsByUser(userId: number, agentId?: number): Promise<SessionListItem[]> {
  const { data } = await request.get(`/sessions/by-user/${userId}`, {
    params: agentId ? { agent_id: agentId } : {},
  })
  return data
}
