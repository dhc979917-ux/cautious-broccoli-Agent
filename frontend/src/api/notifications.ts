import request from './request'

// 站内通知 API（后端 /api/v1/notifications）

export interface NotificationItem {
  id: number
  type: string
  title: string
  content: string | null
  link: string | null
  job_id: number | null
  run_id: number | null
  is_read: boolean
  created_at: string
}

export async function listNotifications(limit = 20): Promise<{ unread: number; items: NotificationItem[] }> {
  const { data } = await request.get('/notifications/', { params: { limit } })
  return data
}

export async function markNotificationRead(id: number): Promise<void> {
  await request.patch(`/notifications/${id}/read`)
}

export async function markAllNotificationsRead(): Promise<void> {
  await request.patch('/notifications/read-all')
}
