import { useEffect, useState, useRef, useCallback } from 'react'
import { Button, Spin, Drawer, Tag, message } from 'antd'
import { ArrowLeftOutlined, RobotOutlined } from '@ant-design/icons'
import { useNavigate, useParams } from 'react-router-dom'
import { useAuthStore } from '../../store/authStore'
import { getSessionsByUser, getSessionPreview, SessionListItem } from '../../api/session'
import { listSessionPreviews } from '../../api/preview'
import { getChunk } from '../../api/knowledge'
import MessageItem from '../Chat/components/MessageItem'
import type { ChatMessage } from '../Chat/types'
// 复用 Chat 页面的消息渲染样式（MessageItem 依赖的 CSS 类如 message-wrapper / md-body / step-tool-box 等定义在 Chat/index.css）
import '../Chat/index.css'
import './index.css'

const ChatPreview = () => {
  const navigate = useNavigate()
  const { userId } = useParams<{ userId?: string }>()
  const token = useAuthStore((s) => s.token)
  const targetUserId = userId ? parseInt(userId, 10) : 0
  const [sessions, setSessions] = useState<SessionListItem[]>([])
  const [currentSessionId, setCurrentSessionId] = useState<number | null>(null)
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [loadingSessions, setLoadingSessions] = useState(false)
  const [loadingMessages, setLoadingMessages] = useState(false)
  const [username, setUsername] = useState<string>('')
  const [agentName, setAgentName] = useState<string>('Agent')
  const [citationDrawer, setCitationDrawer] = useState<{ loading: boolean; docId?: number; chunkIndex?: number; content?: string; filename?: string; title?: string; totalChunks?: number } | null>(null)
  const chatScrollRef = useRef<HTMLDivElement>(null)

  const loadSessions = useCallback(async () => {
    if (!targetUserId) return
    setLoadingSessions(true)
    setCurrentSessionId(null)
    setMessages([])
    setUsername('')
    setAgentName('Agent')
    try {
      const list = await getSessionsByUser(targetUserId)
      setSessions(list)
      if (list.length > 0) setCurrentSessionId(list[0].id)
    } catch (e: any) {
      message.error(e.response?.data?.detail || '加载会话列表失败')
    } finally {
      setLoadingSessions(false)
    }
  }, [targetUserId])

  const loadMessages = useCallback(async (sessionId: number) => {
    setLoadingMessages(true)
    setMessages([])
    try {
      const preview = await getSessionPreview(sessionId, 500)
      const mapped: ChatMessage[] = (preview.messages || []).map((m: any) => ({
        id: m.id,
        role: m.role || (m.is_user ? 'user' : 'agent'),
        content: m.content || '',
        created_at: m.created_at,
        steps: m.steps || [],
        file_outputs: m.file_outputs || [],
        service_previews: m.service_previews || [],
        token_usage: m.token_usage,
        tool_call_count: m.tool_call_count,
        attachments: m.attachments || [],
        elapsed_seconds: m.elapsed_seconds ?? undefined,
      }))
      setMessages(mapped)
      if (preview.session?.username) setUsername(preview.session.username)
      if (preview.session?.agent_name) setAgentName(preview.session.agent_name)
      if (mapped.some((m) => (m.service_previews || []).length > 0)) {
        listSessionPreviews(sessionId)
          .then((res) => {
            if (!res.enabled || !res.items || res.items.length === 0) return
            const fresh = new Map(res.items.map((p) => [p.id, p]))
            setMessages((prev) => prev.map((m) => (m.service_previews || []).length > 0 ? { ...m, service_previews: (m.service_previews || []).map((p) => fresh.get(p.id) || p) } : m))
          })
          .catch(() => undefined)
      }
      setTimeout(() => { if (chatScrollRef.current) chatScrollRef.current.scrollTop = 0 }, 200)
    } catch (e: any) {
      message.error(e.response?.data?.detail || '加载消息失败')
    } finally {
      setLoadingMessages(false)
    }
  }, [])

  useEffect(() => { loadSessions() }, [loadSessions])
  useEffect(() => { if (currentSessionId) loadMessages(currentSessionId) }, [currentSessionId, loadMessages])

  const downloadFile = (url: string, filename: string) => {
    fetch(url, { headers: { Authorization: `Bearer ${token}` } })
      .then((res) => res.blob())
      .then((blob) => {
        const link = document.createElement('a')
        const objUrl = URL.createObjectURL(blob)
        link.href = objUrl
        link.download = filename
        link.click()
        URL.revokeObjectURL(objUrl)
      })
      .catch(() => message.error('下载失败'))
  }
  const handleDownload = (filename: string) => downloadFile(`/api/v1/upload/download/${encodeURIComponent(filename)}`, filename)
  const handleDownloadUploaded = (storedName: string, originalName: string) => downloadFile(`/api/v1/upload/download-upload/${encodeURIComponent(storedName)}`, originalName)

  const onCitationClick = useCallback(async (docId: number, chunkIndex: number) => {
    setCitationDrawer({ loading: true, docId, chunkIndex })
    try {
      const r = await getChunk(docId, chunkIndex)
      setCitationDrawer({ loading: false, docId, chunkIndex, content: r.content, filename: r.filename, title: r.title, totalChunks: r.total_chunks })
    } catch {
      setCitationDrawer({ loading: false, docId, chunkIndex })
      message.error('加载引用内容失败')
    }
  }, [])

  const renderSessionList = () => {
    if (loadingSessions) return <div style={{ display: 'flex', justifyContent: 'center', padding: 40 }}><Spin size="large" /></div>
    if (sessions.length === 0) return (
      <div className="chat-preview-empty">
        <RobotOutlined style={{ fontSize: 48, opacity: 0.3 }} />
        <span>该用户暂无聊天记录</span>
      </div>
    )
    return sessions.map((s) => (
      <div key={s.id} className={`chat-preview-session-item ${currentSessionId === s.id ? 'active' : ''}`} onClick={() => setCurrentSessionId(s.id)}>
        <span className="chat-preview-session-item-title">{s.title}</span>
        <span className="chat-preview-session-item-meta">
          {s.agent_name && <span>{s.agent_name}</span>}
          {s.updated_at && <span>{new Date(s.updated_at).toLocaleDateString('zh-CN')}</span>}
        </span>
      </div>
    ))
  }

  const renderMessages = () => {
    if (loadingMessages) return <div style={{ display: 'flex', justifyContent: 'center', padding: 40 }}><Spin size="large" /></div>
    if (messages.length === 0) return (
      <div className="chat-preview-empty">
        <RobotOutlined style={{ fontSize: 48, opacity: 0.3 }} />
        <span>该会话暂无消息</span>
      </div>
    )
    return [...messages].reverse().map((msg, idx) => {
      const forwardIndex = messages.length - 1 - idx
      return (
        <div key={msg.id ?? `msg-${forwardIndex}`} id={`message-${forwardIndex}`}>
          <MessageItem msg={msg} isStreaming={false} displayName={msg.role === 'user' ? (username || '用户') : agentName} onDownload={handleDownload} onDownloadUpload={handleDownloadUploaded} onCitationClick={onCitationClick} />
        </div>
      )
    })
  }

  const currentSession = sessions.find((s) => s.id === currentSessionId)

  return (
    <>
      <div className="chat-layout">
        <div className="chat-sider">
          <div className="chat-preview-sider-header">
            <div className="chat-preview-sider-header-title">
              <RobotOutlined />
              <span>聊天记录</span>
              <Tag color="orange" className="chat-preview-readonly-tag">只读</Tag>
            </div>
            <div className="chat-preview-sider-header-sub">
              {username ? `用户：${username}` : `用户 #${targetUserId}`}
            </div>
          </div>
          <div style={{ flex: 1, overflowY: 'auto' }}>{renderSessionList()}</div>
        </div>
        <div className="chat-main">
          <div className="chat-content">
            <div className="chat-header">
              <div className="chat-header-left">
                <Button type="text" icon={<ArrowLeftOutlined />} onClick={() => navigate(`/monitoring/user/${targetUserId}`)} className="chat-preview-back">返回监控</Button>
                <span className="chat-header-title">{currentSession ? currentSession.title : '选择会话'}</span>
                {currentSession?.agent_name && <Tag color="blue">{currentSession.agent_name}</Tag>}
              </div>
              <div className="chat-header-actions"><Tag color="default">只读预览模式</Tag></div>
            </div>
            <div className="chat-messages-wrapper">
              <div className="chat-preview-messages" ref={chatScrollRef}>{renderMessages()}</div>
            </div>
          </div>
        </div>
      </div>
      <Drawer open={!!citationDrawer} onClose={() => setCitationDrawer(null)} width={520} title={citationDrawer?.filename || '引用内容'} loading={citationDrawer?.loading}>
        {citationDrawer?.content != null && (
          <div>
            <div className="chat-preview-citation-meta">{citationDrawer.title} | chunk {citationDrawer.chunkIndex} / {citationDrawer.totalChunks}</div>
            <div className="chat-preview-citation-content">{citationDrawer.content}</div>
          </div>
        )}
      </Drawer>
    </>
  )
}

export default ChatPreview
