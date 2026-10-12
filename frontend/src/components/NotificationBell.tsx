import React, { useCallback, useEffect, useRef, useState } from 'react'
import { Badge, Button, Empty, Popover, Spin, Tag } from 'antd'
import { BellOutlined, CheckOutlined } from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'
import {
  listNotifications, markAllNotificationsRead, markNotificationRead,
  NotificationItem,
} from '../api/notifications'
import './NotificationBell.css'

const POLL_MS = 60_000

function timeAgo(ts: string): string {
  const diff = Date.now() - new Date(ts).getTime()
  const m = Math.floor(diff / 60000)
  if (m < 1) return '刚刚'
  if (m < 60) return `${m} 分钟前`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h} 小时前`
  return `${Math.floor(h / 24)} 天前`
}

/** 侧边栏通知铃铛：调度任务执行结果的站内通知（60s 轮询）。
 *  collapsed 由侧边栏传入但不再影响渲染——铃铛始终只显示图标（无"通知"文字）。 */
const NotificationBell: React.FC<{ collapsed?: boolean }> = () => {
  const navigate = useNavigate()
  const [items, setItems] = useState<NotificationItem[]>([])
  const [unread, setUnread] = useState(0)
  const [open, setOpen] = useState(false)
  const [loading, setLoading] = useState(false)
  const timer = useRef<number | null>(null)

  const load = useCallback(async (silent = true) => {
    if (!silent) setLoading(true)
    try {
      const res = await listNotifications(20)
      setItems(res.items)
      setUnread(res.unread)
    } catch { /* 静默：通知失败不影响主流程 */ }
    if (!silent) setLoading(false)
  }, [])

  useEffect(() => {
    load()
    timer.current = window.setInterval(() => {
      if (document.visibilityState === 'visible') load()
    }, POLL_MS)
    return () => { if (timer.current) window.clearInterval(timer.current) }
  }, [load])

  const onItemClick = async (n: NotificationItem) => {
    if (!n.is_read) {
      try {
        await markNotificationRead(n.id)
        setItems((prev) => prev.map((x) => (x.id === n.id ? { ...x, is_read: true } : x)))
        setUnread((u) => Math.max(0, u - 1))
      } catch { /* ignore */ }
    }
    if (n.link) {
      setOpen(false)
      navigate(n.link)
    }
  }

  const onReadAll = async () => {
    try {
      await markAllNotificationsRead()
      setItems((prev) => prev.map((x) => ({ ...x, is_read: true })))
      setUnread(0)
    } catch { /* ignore */ }
  }

  const content = (
    <div className="notif-panel">
      <div className="notif-panel-head">
        <span className="notif-panel-title">通知</span>
        <Button type="link" size="small" icon={<CheckOutlined />} onClick={onReadAll} disabled={unread === 0}>
          全部已读
        </Button>
      </div>
      <div className="notif-panel-list">
        {loading && items.length === 0 ? (
          <div className="notif-empty"><Spin size="small" /></div>
        ) : items.length === 0 ? (
          <div className="notif-empty"><Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无通知" /></div>
        ) : (
          items.map((n) => (
            <div
              key={n.id}
              className={`notif-item ${n.is_read ? '' : 'unread'}`}
              onClick={() => onItemClick(n)}
            >
              <div className="notif-item-title">
                <Tag color={n.title.includes('成功') ? 'success' : n.title.includes('失败') || n.title.includes('超时') ? 'error' : 'default'}>
                  {n.title.includes('成功') ? '成功' : n.title.includes('失败') ? '失败' : n.title.includes('超时') ? '超时' : '通知'}
                </Tag>
                <span className="notif-item-name">{n.title}</span>
                <span className="notif-item-time">{timeAgo(n.created_at)}</span>
              </div>
              {n.content && <div className="notif-item-content">{n.content}</div>}
              {n.link && <div className="notif-item-link">点击查看会话 →</div>}
            </div>
          ))
        )}
      </div>
    </div>
  )

  return (
    <Popover
      content={content}
      trigger="click"
      open={open}
      onOpenChange={(v) => { setOpen(v); if (v) load(false) }}
      placement="topRight"
      overlayClassName="notif-popover"
    >
      <div className="notif-bell" title="通知">
        <Badge count={unread} size="small" offset={[-2, 2]}>
          <BellOutlined className="notif-bell-icon" />
        </Badge>
      </div>
    </Popover>
  )
}

export default NotificationBell
