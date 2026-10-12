// 会话只读预览浮窗：文件中心"来源"列与调度任务执行历史共用。
// 复用聊天页的 MessageItem 渲染，思考过程/工具调用/产出文件与聊天页保持一致；
// 他人会话不传下载回调，产出与附件的下载入口自动隐藏（避免点了就 403 的死链）。
import { useCallback, useEffect, useRef, useState } from 'react'
import { Button, Empty, Modal, Skeleton, Tag } from 'antd'
import {
  CommentOutlined, ExportOutlined, RobotOutlined, UserOutlined,
} from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'
import { getSessionPreview, SessionPreview } from '../../api/session'
import { useAuthStore } from '../../store/authStore'
import MessageItem from '../../pages/Chat/components/MessageItem'
// 气泡样式沿用聊天页（其中选择器全部为 class 作用域，不会影响其他页面布局）
import '../../pages/Chat/index.css'
import './index.css'

interface SessionPreviewModalProps {
  /** 目标会话 id；null 表示关闭 */
  sessionId: number | null
  onClose: () => void
  /** 从 Drawer 等高层级弹层里打开时抬高层级，避免浮窗被压在下面 */
  zIndex?: number
}

function fmtTime(s?: string | null): string {
  if (!s) return ''
  const d = new Date(s)
  return Number.isNaN(d.getTime()) ? '' : d.toLocaleString('zh-CN', { hour12: false })
}

export default function SessionPreviewModal({ sessionId, onClose, zIndex }: SessionPreviewModalProps) {
  const navigate = useNavigate()
  const token = useAuthStore((s) => s.token)
  const [data, setData] = useState<SessionPreview | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const bodyRef = useRef<HTMLDivElement>(null)
  // 只接受最新一次请求的结果：连续点不同文件的来源时，避免旧响应覆盖新会话
  const reqSeq = useRef(0)

  useEffect(() => {
    if (sessionId === null) {
      reqSeq.current += 1
      setData(null)
      setError('')
      setLoading(false)
      return
    }
    const seq = ++reqSeq.current
    setLoading(true)
    setError('')
    getSessionPreview(sessionId)
      .then((res) => { if (seq === reqSeq.current) setData(res) })
      .catch((e: any) => {
        if (seq !== reqSeq.current) return
        setData(null)
        setError(e?.response?.status === 403 ? '无权查看该会话' : '会话不存在或已删除')
      })
      .finally(() => { if (seq === reqSeq.current) setLoading(false) })
  }, [sessionId])

  // 内容就位后落到最新一条，与聊天页的阅读落点一致
  useEffect(() => {
    if (!data || !bodyRef.current) return
    bodyRef.current.scrollTop = bodyRef.current.scrollHeight
  }, [data])

  const download = useCallback((url: string, filename: string) => {
    fetch(url, { headers: { Authorization: `Bearer ${token}` } })
      .then((res) => { if (!res.ok) throw new Error(); return res.blob() })
      .then((blob) => {
        const link = document.createElement('a')
        const objUrl = URL.createObjectURL(blob)
        link.href = objUrl
        link.download = filename
        link.click()
        URL.revokeObjectURL(objUrl)
      })
      .catch(() => { /* 预览态下载失败不打断阅读 */ })
  }, [token])

  const meta = data?.session
  const isOwn = !!meta?.is_own
  const messages = data?.messages || []

  return (
    <Modal
      open={sessionId !== null}
      onCancel={onClose}
      width="min(920px, 92vw)"
      className="session-preview-modal"
      destroyOnClose
      zIndex={zIndex}
      title={
        <div className="sp-head">
          <div className="sp-title">
            <CommentOutlined />
            <span className="sp-title-text" title={meta?.title || ''}>
              {meta?.title || '会话预览'}
            </span>
          </div>
          {meta && (
            <div className="sp-meta">
              {meta.agent_name && <Tag icon={<RobotOutlined />}>{meta.agent_name}</Tag>}
              {!isOwn && (
                <Tag icon={<UserOutlined />} color="blue">
                  {meta.username || `用户 #${meta.user_id}`}
                </Tag>
              )}
              {meta.created_at && <span className="sp-meta-item">{fmtTime(meta.created_at)}</span>}
              {!loading && !error && (
                <span className="sp-meta-item">{messages.length} 条消息</span>
              )}
            </div>
          )}
        </div>
      }
      footer={meta && isOwn ? (
        <Button
          type="primary"
          icon={<ExportOutlined />}
          onClick={() => {
            onClose()
            navigate(`/chat/${meta.id}?agent=${meta.agent_id ?? ''}`)
          }}
        >
          在聊天中打开
        </Button>
      ) : null}
    >
      <div className="sp-body" ref={bodyRef}>
        {loading && <Skeleton active paragraph={{ rows: 6 }} />}
        {!loading && error && (
          <div className="sp-error" role="alert">
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={error} />
          </div>
        )}
        {!loading && !error && data && (
          messages.length === 0 ? (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="该会话还没有消息" />
          ) : (
            <div className="sp-messages">
              {data.has_more && (
                <div className="sp-more-hint">仅显示最近 {messages.length} 条消息</div>
              )}
              {messages.map((m, i) => (
                <MessageItem
                  key={m.id ?? `sp-${i}`}
                  msg={m}
                  isStreaming={false}
                  displayName={m.role === 'user'
                    ? (meta?.username || '用户')
                    : (meta?.agent_name || 'Agent')}
                  onDownload={isOwn
                    ? (filename) => download(`/api/v1/upload/download/${encodeURIComponent(filename)}`, filename)
                    : undefined}
                  onDownloadUpload={isOwn
                    ? (stored, original) => download(
                      `/api/v1/upload/download-upload/${encodeURIComponent(stored)}`, original)
                    : undefined}
                />
              ))}
            </div>
          )
        )}
      </div>
    </Modal>
  )
}
