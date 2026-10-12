import { useState, useEffect, useRef, useCallback, useMemo } from 'react'
import { Alert, Button, Input, message, Tooltip, Modal, Upload, Spin, Dropdown, Badge, Drawer } from 'antd'
import {
  SendOutlined,
  PlusOutlined,
  DeleteOutlined,
  EditOutlined,
  MenuOutlined,
  PaperClipOutlined,
  RobotOutlined,
  LoadingOutlined,
  ApiOutlined,
  BulbOutlined,
  ScheduleOutlined,
  AppstoreOutlined,
  CloseOutlined,
} from '@ant-design/icons'
import { useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { useAuthStore } from '../../store/authStore'
import { useDraftStore } from '../../store/draftStore'
import request from '../../api/request'
import { getAllSkills, SkillInfo } from '../../api/skill'
import { Session, ChatMessage, UploadedFile, PendingInput, IntakeQuestion, TokenUsage } from './types'
import { summarizeTokenUsage } from '../../utils/tokenUsage'
import { getWsBaseUrl } from '../../utils/ws'
import MessageItem from './components/MessageItem'
import CacheIndicator from '../../components/CacheIndicator'
import { getChunk } from '../../api/knowledge'
import SkillPanel from './components/SkillPanel'
import SkillEvolutionPanel from '../../components/SkillEvolutionPanel'
import { evolutionBindings, EvolutionBinding } from '../../api/skillEvolution'
import ChatComposer from './components/ChatComposer'
import AskUserBar from './components/AskUserBar'
import RequirementsForm from './components/RequirementsForm'
import { listSessionPreviews } from '../../api/preview'
import ProgressRing from '../../components/ProgressRing'
import MessageNavPanel from './components/MessageNavPanel'
import McpPickerPanel from './components/McpPickerPanel'
import PlanPanel from './components/PlanPanel'
import ScrollEdgeButton, { scrollToEdge } from './components/ScrollEdgeButton'
import { listMcpServers, McpServerInfo } from '../../api/mcp'
import { forceLogout, REASON_SESSION_REVOKED } from '../../utils/forceLogout'
import './index.css'

const Chat = () => {
  const navigate = useNavigate()
  const { sessionId: routeSessionId } = useParams<{ sessionId?: string }>()

  const token = useAuthStore((s) => s.token)
  const user = useAuthStore((s) => s.user)
  const selectedAgentId = useAuthStore((s) => s.selectedAgentId)
  const setSelectedAgentId = useAuthStore((s) => s.setSelectedAgentId)
  const [searchParams] = useSearchParams()
  const selectedSkills = useAuthStore((s) => s.selectedSkills)
  const [personalBindings, setPersonalBindings] = useState<EvolutionBinding[]>([])
  const setSelectedSkills = useAuthStore((s) => s.setSelectedSkills)
  const selectedMcps = useAuthStore((s) => s.selectedMcps)
  const setSelectedMcps = useAuthStore((s) => s.setSelectedMcps)

  // ===== State =====
  const [sessions, setSessions] = useState<Session[]>([])
  const [availableAgents, setAvailableAgents] = useState<{ id: number; name: string }[]>([])
  const selectedAgentRef = useRef(selectedAgentId)
  selectedAgentRef.current = selectedAgentId
  const [currentSessionId, setCurrentSessionId] = useState<number | null>(null)
  const currentSessionIdRef = useRef<number | null>(null)
  const prevSessionIdRef = useRef<number | null>(null)

  // Keep ref in sync
  useEffect(() => { currentSessionIdRef.current = currentSessionId }, [currentSessionId])

  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [inputValue, setInputValue] = useState('')
  const [loading, setLoading] = useState(false)
  const [streaming, setStreaming] = useState(false)
  const [compressingSessionId, setCompressingSessionId] = useState<number | null>(null)
  const [visibleMessagesSessionId, setVisibleMessagesSessionId] = useState<number | null>(null)
  const [liveUsage, setLiveUsage] = useState<{ sessionId: number; runId: string; usage: TokenUsage } | null>(null)
  const [storedUsage, setStoredUsage] = useState<{
    sessionId: number; usage: TokenUsage | null; lastRunId?: string
  } | null>(null)
  const sessionUsage = useMemo(() => {
    const stored = storedUsage?.sessionId === currentSessionId ? storedUsage : null
    const live = liveUsage?.sessionId === currentSessionId && liveUsage.runId !== stored?.lastRunId
      ? [{ run_id: liveUsage.runId, token_usage: liveUsage.usage }] : []
    if (stored?.usage) {
      return summarizeTokenUsage([{ run_id: 'stored', token_usage: stored.usage }, ...live])
    }
    return summarizeTokenUsage([
      ...(visibleMessagesSessionId === currentSessionId ? messages : []), ...live,
    ])
  }, [messages, visibleMessagesSessionId, currentSessionId, liveUsage, storedUsage])
  // 进行中阶段文案（后端 status 事件 / 工具调用派生），与转圈圆环一起渲染
  const [statusText, setStatusText] = useState('')
  const [siderCollapsed, setSiderCollapsed] = useState(false)
  const [skills, setSkills] = useState<SkillInfo[]>([])
  const [editingSessionId, setEditingSessionId] = useState<number | null>(null)
  const [editingTitle, setEditingTitle] = useState('')
  const [uploadedFiles, setUploadedFiles] = useState<UploadedFile[]>([])
  const [uploadingCount, setUploadingCount] = useState(0)
  // 任务运行中排队（延时发送）的输入：发送后挂在这里，下一轮 tool_calls 注入给模型
  const [pendingInputs, setPendingInputs] = useState<PendingInput[]>([])
  const pendingInputsRef = useRef<PendingInput[]>([])
  useEffect(() => { pendingInputsRef.current = pendingInputs }, [pendingInputs])
  // 可选模型列表（后端 AVAILABLE_MODELS 配置）与本次对话所选模型（''=用 Agent 默认模型）
 const [models, setModels] = useState<string[]>([])
 const [model, setModel] = useState<string>('')
 const [defaultModel, setDefaultModel] = useState<string>('')
  const [usingPreviousConfig, setUsingPreviousConfig] = useState(false)
  const [runtimeError, setRuntimeError] = useState('')
  const [runtimeLoading, setRuntimeLoading] = useState(true)
 const [agentName, setAgentName] = useState('Agent')
  // Right-side drawer panel: plan / mcp / skill / nav
  const [activePanel, setActivePanel] = useState<'plan' | 'mcp' | 'skill' | 'nav' | null>(null)
  const [citationDrawer, setCitationDrawer] = useState<{ loading: boolean; docId?: number; chunkIndex?: number; content?: string; filename?: string; title?: string; totalChunks?: number } | null>(null)
  // Full MCP server list + MCP ids bound to the current Agent (from Agent.config.mcp_servers)
  const [mcpServers, setMcpServers] = useState<McpServerInfo[]>([])
  const [boundMcpIds, setBoundMcpIds] = useState<number[]>([])

  // Refs
  const messagesEndRef = useRef<HTMLDivElement>(null)
  const chatScrollRef = useRef<HTMLDivElement>(null)
  const wsRef = useRef<WebSocket | null>(null)
  const streamingSessionIdRef = useRef<number | null>(null)
  const streamingMsgRef = useRef<ChatMessage | null>(null)
  const streamingMsgIndexRef = useRef<number>(-1)
  const autoFollowRef = useRef(true)
  const programmaticScrollUntilRef = useRef(0)
  const lastChatScrollTopRef = useRef(0)
  const lastTouchYRef = useRef<number | null>(null)
  // 切换会话时草稿还原会触发一次保存 effect，用计数器跳过该次保存，
  // 避免把上一个会话的输入内容写进新会话的草稿
  const skipDraftSaveRef = useRef(0)

  // ===== Scroll to bottom =====
  // 消息容器是 column-reverse（scrollTop=0 即底边，向上滚为负值）。
  // force=true：发送/投递/加载历史等主动场景重置跟随状态并到底；
  // force=false：流式输出尊重用户当前阅读位置
  const scrollToBottom = useCallback((force = false) => {
    if (force) autoFollowRef.current = true
    if (!force && !autoFollowRef.current) return
    setTimeout(() => {
      const el = chatScrollRef.current
      if (!el) return
      programmaticScrollUntilRef.current = Date.now() + 100
      el.scrollTop = el.scrollHeight
    }, 50)
  }, [])

  // 滚动圆钮的「回到顶部」：与 scrollToBottom 对称。滚动前先断开自动跟随，
  // 否则流式输出会立刻把视图拽回底部；方向/符号语义统一由 scrollToEdge 处理。
  const scrollToTop = useCallback(() => {
    const el = chatScrollRef.current
    if (!el) return
    autoFollowRef.current = false
    scrollToEdge(el, 'top')
  }, [])

  useEffect(() => {
    const el = chatScrollRef.current
    if (!el) return

    const isNearBottom = () => Math.abs(el.scrollTop) <= 24
    const handleScroll = () => {
      const current = el.scrollTop
      if (Date.now() < programmaticScrollUntilRef.current) {
        lastChatScrollTopRef.current = current
        return
      }
      const movedUp = current < lastChatScrollTopRef.current - 2
      if (movedUp || Math.abs(current) > 24) {
        autoFollowRef.current = false
      } else if (isNearBottom()) {
        autoFollowRef.current = true
      }
      lastChatScrollTopRef.current = current
    }
    const handleWheel = (event: WheelEvent) => {
      if (event.deltaY < 0) autoFollowRef.current = false
    }
    const handleTouchStart = (event: TouchEvent) => {
      lastTouchYRef.current = event.touches[0]?.clientY ?? null
    }
    const handleTouchMove = (event: TouchEvent) => {
      const y = event.touches[0]?.clientY
      if (y != null && lastTouchYRef.current != null && y < lastTouchYRef.current) {
        autoFollowRef.current = false
      }
      lastTouchYRef.current = y
    }

    el.addEventListener('scroll', handleScroll)
    el.addEventListener('wheel', handleWheel, { passive: true })
    el.addEventListener('touchstart', handleTouchStart, { passive: true })
    el.addEventListener('touchmove', handleTouchMove, { passive: true })
    return () => {
      el.removeEventListener('scroll', handleScroll)
      el.removeEventListener('wheel', handleWheel)
      el.removeEventListener('touchstart', handleTouchStart)
      el.removeEventListener('touchmove', handleTouchMove)
    }
  }, [])

  // ===== Load sessions =====
  const loadSessions = useCallback(async () => {
    if (!token) return
    try {
      // 按当前 Agent 过滤：不同 Agent 的历史记录互相隔离
      const res = await request.get('/sessions/', {
        params: selectedAgentId ? { agent_id: selectedAgentId } : {},
      })
      const list: Session[] = Array.isArray(res.data) ? res.data : []
      if (selectedAgentRef.current !== selectedAgentId) return
      setSessions(list)

      // Auto-select from route or first session
      if (routeSessionId) {
        const id = parseInt(routeSessionId, 10)
        if (list.find((s) => s.id === id)) {
          setCurrentSessionId(id)
        }
      } else if (list.length > 0 && !currentSessionIdRef.current) {
        setCurrentSessionId(list[0].id)
      }
    } catch (err) {
      console.error('Failed to load sessions', err)
    }
  }, [token, routeSessionId, selectedAgentId])

  useEffect(() => {
    if (!token) return
    request.get('/agents/').then((res) => {
      setAvailableAgents(Array.isArray(res.data) ? res.data : [])
    }).catch(() => message.error('加载 Agent 列表失败'))
  }, [token])

  const switchAgent = (agentId: number) => {
    if (agentId === selectedAgentId) return
    if (currentSessionIdRef.current) {
      useDraftStore.getState().saveDraft(currentSessionIdRef.current, {
        text: inputValue, files: uploadedFiles, model, pending: pendingInputs,
      })
    }
    if (wsRef.current) {
      wsRef.current.close()
      wsRef.current = null
    }
    currentSessionIdRef.current = null
    setCurrentSessionId(null)
    setSessions([])
    setMessages([])
    setVisibleMessagesSessionId(null)
    setLiveUsage(null)
    setStoredUsage(null)
    setUploadedFiles([])
    setPendingInputs([])
    setInputValue('')
    setModel('')
    setStreaming(false)
    setSelectedMcps(null)
    setSelectedSkills([])
    setSelectedAgentId(agentId)
    navigate('/chat', { replace: true })
  }

  // ===== Load agent info =====
  const loadAgentInfo = useCallback(async () => {
    if (!selectedAgentId) return
    setRuntimeLoading(true)
    try {
      const res = await request.get(`/agents/${selectedAgentId}/runtime`, { params: { session_id: currentSessionId || undefined } })
      if (selectedAgentId !== useAuthStore.getState().selectedAgentId || currentSessionId !== currentSessionIdRef.current) return
      if (res.data?.name) {
        setAgentName(res.data.name)
      }
      // MCP ids bound to this Agent (admin-configured); drives the MCP picker scope
      const bound: any[] = Array.isArray(res.data?.mcp_servers) ? res.data.mcp_servers : []
      setBoundMcpIds(bound.map((i) => Number(i)).filter((i) => !Number.isNaN(i)))
      setDefaultModel(res.data.default_model || '')
      setModels(res.data.models || [])
      setUsingPreviousConfig(!res.data.is_current)
      setRuntimeError('')
    } catch (err) {
      if (selectedAgentId !== useAuthStore.getState().selectedAgentId || currentSessionId !== currentSessionIdRef.current) return
      setRuntimeError('无法获取会话生效配置，请刷新重试。')
      setBoundMcpIds([])
      console.error('Failed to load agent info', err)
    } finally {
      if (selectedAgentId === useAuthStore.getState().selectedAgentId && currentSessionId === currentSessionIdRef.current) setRuntimeLoading(false)
    }
  }, [selectedAgentId, currentSessionId])

  const refreshPersonalSkills = useCallback(async () => {
    if (!currentSessionId) { setPersonalBindings([]); return }
    try {
      const bindings = await evolutionBindings(currentSessionId)
      if (currentSessionIdRef.current !== currentSessionId) return
      setPersonalBindings(bindings)
      await loadSkills()
    } catch { /* Optional evolution service must not interrupt chat. */ }
  }, [currentSessionId])
  useEffect(() => { setPersonalBindings([]); void refreshPersonalSkills() }, [refreshPersonalSkills])

  // ===== Load MCP servers =====
  const loadMcpServers = useCallback(async () => {
    try {
      setMcpServers(await listMcpServers())
    } catch (err) {
      console.error('Failed to load MCP servers', err)
    }
  }, [])

  // ===== Load skills =====
  const loadSkills = useCallback(async () => {
    try {
      const list = await getAllSkills()
      const available = list.filter((s) => s.is_active && s.has_access)
      setSkills(available)
      // 清理已失效的选中（技能被删除或当前用户无权限），保留仍有效的选择
      const names = available.map((s) => s.name)
      const current = useAuthStore.getState().selectedSkills
      const stillValid = current.filter((n) => names.includes(n))
      if (stillValid.length !== current.length) {
        setSelectedSkills(stillValid)
      }
    } catch (err) {
      console.error('Failed to load skills', err)
    }
  }, [setSelectedSkills])

  // ===== Load messages for a session =====
  const loadMessages = useCallback(async (sid: number) => {
    setLoading(true)
    try {
      const res = await request.get(`/sessions/${sid}/messages`, { params: { include_usage: true } })
      if (currentSessionIdRef.current !== sid) return
      const totals: (TokenUsage & { last_run_id?: string }) | null = res.data.usage_totals || null
      setStoredUsage({ sessionId: sid, usage: totals, lastRunId: totals?.last_run_id })
      if (totals?.last_run_id) {
        setLiveUsage((prev) => prev?.sessionId === sid && prev.runId === totals.last_run_id ? null : prev)
      }
      const list: any[] = Array.isArray(res.data.messages) ? res.data.messages : []
      const mapped: ChatMessage[] = list.map((m) => ({
        id: m.id,
        role: m.role || (m.is_user ? 'user' : 'agent'),
        content: m.content || '',
        created_at: m.created_at,
        steps: m.steps || [],
        file_outputs: m.file_outputs || [],
        service_previews: m.service_previews || [],
        token_usage: m.token_usage,
        run_id: m.run_id,
        tool_call_count: m.tool_call_count,
        attachments: m.attachments || [],
        elapsed_seconds: m.elapsed_seconds ?? undefined,
      }))
      setMessages(mapped)
      setVisibleMessagesSessionId(sid)
      // 服务预览状态以库里最新为准：消息 metadata 只是发布当时的快照，
      // 沙箱回收/到期后卡片必须显示"已失效"，不能继续给一个点不开的死链。
      // 接口不可用（未开启预览/网络问题）时静默跳过，不影响历史消息渲染。
      if (mapped.some((m) => (m.service_previews || []).length > 0)) {
        listSessionPreviews(sid)
          .then((res) => {
            if (currentSessionIdRef.current !== sid) return
            if (!res.enabled || !res.items || res.items.length === 0) return
            const fresh = new Map(res.items.map((p) => [p.id, p]))
            setMessages((prev) =>
              prev.map((m) =>
                (m.service_previews || []).length > 0
                  ? {
                      ...m,
                      service_previews: (m.service_previews || []).map((p) => fresh.get(p.id) || p),
                    }
                  : m,
              ),
            )
          })
          .catch(() => undefined)
      }
      setTimeout(() => scrollToBottom(true), 200)
    } catch (err) {
      console.error('Failed to load messages', err)
    } finally {
      setLoading(false)
    }
  }, [scrollToBottom])

  // ===== WebSocket 事件处理（发送与续播共用）=====
  // sendStartTs：正常发送时的本地计时起点；resume 续播传 null（用时以落库 metadata 为准）
  const attachWsHandlers = useCallback((ws: WebSocket, sendStartTs: number | null, sid: number) => {
    ws.onmessage = (event) => {
      if (wsRef.current !== ws || currentSessionIdRef.current !== sid) return
      let data: any
      try {
        data = JSON.parse(event.data)
      } catch {
        return
      }

      // ===== 控制类事件（与流式消息本体无关）：就地处理并返回，不走尾部通用更新 =====
      if (data.type === 'skill_evolution') { void loadSkills(); return }
      if (data.type === 'status') {
        // 构建阶段（记忆库 / MCP / 沙箱）进度事件：刷新进行中指示文案，
        // 让用户在长时间无思考流输出时也知道任务仍在推进
        setStatusText(data.content || '')
        return
      }

      // 管理员改动了本账号的角色/权限：服务端已吊销 token 并停掉在跑的任务，
      // 这里立刻整页下线（data.reason 仅用于日志，跳转原因统一按"权限变更"提示）
      if (data.type === 'force_logout') {
        console.info('[Chat] 收到服务端下线通知', data.reason || '')
        try { ws.close() } catch { /* 关闭失败不影响下线 */ }
        forceLogout(REASON_SESSION_REVOKED)
        return
      }

      if (data.type === 'queued') {
        // 排队确认：前端已乐观更新（queue_input 发送时即更新本地待发送区），无需处理
        return
      }

      if (data.type === 'queued_rejected') {
        // 后端当前无运行中任务（发送瞬间任务刚好结束的小概率窗口）：改走普通发送
        const rejectedItems: PendingInput[] = (data.items || []).map((it: any) => ({
          content: it.content || '',
          files: (it.files || []).map((f: any) => ({
            file_path: f.path || '',
            stored_name: f.stored_name || '',
            original_name: f.name || '',
          })),
        }))
        setStreaming(false)
        setPendingInputs([])
        sendMergedRef.current?.(rejectedItems)
        return
      }

      if (data.type === 'queued_delivered') {
        // 排队内容已在本轮模型调用前注入：分段展示——
        // 把当前流式消息（到此为止的思考）冻结为独立一条，用户消息插在当前位置，
        // 新的占位消息接管后续事件，视觉上"思考在用户消息之后继续"
        const delivered: { content: string; attachments?: { name: string; stored_name: string }[] }[] =
          data.items || []
        if (delivered.length > 0) {
          const userMsgs: ChatMessage[] = delivered.map((it) => ({
            role: 'user',
            content: it.content || '',
            attachments: it.attachments || [],
          }))
          const sIdx = streamingMsgIndexRef.current
          const frozen: ChatMessage = {
            ...(streamingMsgRef.current || { role: 'agent', content: '', steps: [], file_outputs: [] }),
          }
          frozen.token_usage = undefined
          const fresh: ChatMessage = {
            role: 'agent', content: '', steps: [], file_outputs: [],
            run_id: streamingMsgRef.current?.run_id,
            token_usage: streamingMsgRef.current?.token_usage,
          }
          // ref 的读写放在 setMessages 之外（StrictMode 双调用 updater）
          streamingMsgRef.current = fresh
          streamingMsgIndexRef.current = sIdx + userMsgs.length + 1
          setMessages((prev) => {
            if (sIdx < 0 || sIdx >= prev.length) return prev
            return [...prev.slice(0, sIdx), frozen, ...userMsgs, fresh]
          })
          const deliveredKeys = new Set(delivered.map((it) => it.content || ''))
          setPendingInputs((prev) => prev.filter((p) => !deliveredKeys.has(p.content)))
          scrollToBottom(true)
        }
        return
      }

      if (data.type === 'stop_ack') {
        // 后端已停旧任务；若带排队内容，已作为新任务重新发出，事件流继续在本连接推送
        const ackItems: { content: string; attachments?: { name: string; stored_name: string }[] }[] =
          data.items || []
        setPendingInputs([])
        setInputValue('')
        setUploadedFiles([])
        if (ackItems.length > 0) {
          const userMsgs: ChatMessage[] = ackItems.map((it) => ({
            role: 'user',
            content: it.content || '',
            attachments: it.attachments || [],
            created_at: new Date().toISOString(),
          }))
          const agentMsg: ChatMessage = { role: 'agent', content: '', steps: [], file_outputs: [] }
          streamingMsgRef.current = agentMsg
          setMessages((prev) => {
            const next = [...prev, ...userMsgs, agentMsg]
            streamingMsgIndexRef.current = next.length - 1
            return next
          })
          setStreaming(true)
          scrollToBottom(true)
        } else {
          streamingMsgRef.current = null
          streamingMsgIndexRef.current = -1
        }
        // 刷新消息列表：让前端拿到 DB id（分支/撤销操作依赖 msg.id）
        if (currentSessionIdRef.current) {
          loadMessagesRef.current(currentSessionIdRef.current).catch(() => {})
        }
        return
      }

      const msg = streamingMsgRef.current
      if (!msg) return

      // 思考事件强制滚动标记：让用户始终看到最新生成的思考内容
      let forceScroll = false

      switch (data.type) {
        case 'thinking': {
          // 思考事件：强制滚动到底部，让用户始终看到最新生成的思考内容
          forceScroll = true
          setStatusText('思考中…')
          // 连续的思考增量合并为同一段，避免一个 token 一行
          const steps = [...(msg.steps || [])]
          const last = steps[steps.length - 1]
          if (last && last.type === 'thinking') {
            steps[steps.length - 1] = { ...last, content: (last.content || '') + (data.content || '') }
          } else {
            steps.push({ type: 'thinking', content: data.content })
          }
          msg.steps = steps
          break
        }

        case 'tool_call':
          if (data.status === 'calling') {
            setStatusText(`正在执行 ${data.name}…`)
            msg.steps = [
              ...(msg.steps || []),
              { type: 'tool_call', tool_call_id: data.tool_call_id, name: data.name, args: data.args, status: 'calling', round: data.round },
            ]
          } else if (data.status === 'done') {
            // Update the last tool_call with done status
            const steps = [...(msg.steps || [])]
            for (let i = steps.length - 1; i >= 0; i--) {
              if (steps[i].type === 'tool_call' && steps[i].tool_call_id === data.tool_call_id && steps[i].status === 'calling') {
                steps[i] = { ...steps[i], status: 'done' }
                break
              }
            }
            msg.steps = steps
          }
          break

        case 'tool_result': {
          setStatusText('思考中…')
          // 工具实际输出：挂到最近一个同名 tool_call step（小框展示命令结果）
          const steps = [...(msg.steps || [])]
          for (let i = steps.length - 1; i >= 0; i--) {
            if (steps[i].type === 'tool_call' && steps[i].tool_call_id === data.tool_call_id && data.tool_call_id) {
              steps[i] = { ...steps[i], output: data.output, state: data.state, status: 'done' }
              break
            }
          }
          msg.steps = steps
          break
        }

        case 'chunk':
          msg.content += data.content || ''
          break

        case 'tasks': {
          // 官方任务列表快照（Task 工具产出）：只保留最新一份，PlanPanel 渲染
          const tSteps = [...(msg.steps || [])]
          const tIdx = tSteps.findIndex((s) => s.type === 'tasks')
          if (tIdx >= 0) {
            tSteps[tIdx] = { type: 'tasks', tasks: data.tasks || [] }
          } else {
            tSteps.push({ type: 'tasks', tasks: data.tasks || [] })
          }
          msg.steps = tSteps
          break
        }

        case 'file_output':
          msg.file_outputs = [
            ...(msg.file_outputs || []),
            { filename: data.filename, download_url: data.download_url, size: data.size },
          ]
          break

        case 'service_preview':
          // Agent 在沙箱里起的服务已被平台发布成可直接访问的临时地址
          msg.service_previews = [...(msg.service_previews || []), data.preview]
          break

        case 'require_external_execution': {
          // 官方外部执行事件：回复暂停，按 tool_calls 渲染交互卡
          //   AskUserChoice       → 方案单选条（kind=choice）
          //   CollectRequirements → 需求采集表单（kind=intake）
          // 参数都在 tool_call.input（JSON 字符串）中；作答后经同一条 external_reply 回传
          const extSteps = [...(msg.steps || [])]
          for (const tc of data.tool_calls || []) {
            if (tc.name !== 'AskUserChoice' && tc.name !== 'CollectRequirements') continue
            let parsed: {
              question?: string
              context?: string
              options?: { label?: string; description?: string }[]
              questions?: IntakeQuestion[]
            } = {}
            try {
              parsed = typeof tc.input === 'string' ? JSON.parse(tc.input || '{}') : (tc.input || {})
            } catch {
              parsed = {}
            }
            if (tc.name === 'CollectRequirements') {
              extSteps.push({
                type: 'external_ask',
                kind: 'intake',
                request_id: tc.id,
                reply_id: data.reply_id,
                question: parsed.context || '',
                questions: (parsed.questions || []).slice(0, 8),
                timeout_seconds: data.timeout_seconds || undefined,
              })
            } else {
              const opts = (parsed.options || []).slice(0, 6).map((o, i) => ({
                key: String.fromCharCode(65 + i),
                label: o.label || ('方案' + String.fromCharCode(65 + i)),
                description: o.description || '',
              }))
              extSteps.push({
                type: 'external_ask',
                kind: 'choice',
                request_id: tc.id,
                reply_id: data.reply_id,
                question: parsed.question || '',
                options: opts,
                timeout_seconds: data.timeout_seconds || undefined,
              })
            }
          }
          msg.steps = extSteps
          break
        }

        case 'external_resolved': {
          // 决策卡片已被解决（用户选择 / 超时自动选择 / 跳过 / 中断）
          const askSteps = [...(msg.steps || [])]
          for (let i = askSteps.length - 1; i >= 0; i--) {
            if (askSteps[i].type === 'external_ask' && askSteps[i].request_id === data.request_id) {
              askSteps[i] = {
                ...askSteps[i],
                answer: data.answer ?? undefined,
                resolved_by: data.resolved_by,
              }
              break
            }
          }
          msg.steps = askSteps
          break
        }

        case 'usage':
          msg.token_usage = data.token_usage
          msg.run_id = data.run_id
          if (data.run_id && data.token_usage) {
            setLiveUsage({ sessionId: sid, runId: data.run_id, usage: data.token_usage })
          }
          break

        case 'done':
          msg.token_usage = data.token_usage
          msg.run_id = data.run_id
          msg.tool_call_count = data.tool_call_count
          if (data.run_id && data.token_usage) {
            setLiveUsage({ sessionId: sid, runId: data.run_id, usage: data.token_usage })
          }
          break

        case 'stopped':
          // 旧任务被用户停止（Esc）：收尾本条回复。连接不关闭，等 stop_ack / 新任务事件流
          setStreaming(false)
          setStatusText('')
          // 刷新消息列表：让前端拿到 DB id（分支/撤销操作依赖 msg.id）
          // stopped 事件不刷新是"撤回/分支需刷新页面"bug 的根因
          if (currentSessionIdRef.current) {
            loadMessagesRef.current(currentSessionIdRef.current).catch(() => {})
          }
          break

        case 'error':
          // 决策卡回复未命中（卡片过期/任务已结束）：业务性错误，只提示，
          // 不断开连接、不解除流式状态，避免误杀仍在进行的会话
          if (data.kind === 'external_reply_miss') {
            message.warning(data.content || '未找到对应的决策请求')
            break
          }
          msg.content += `\n\n**错误：** ${data.content || '未知错误'}`
          // 出错也要解除输入锁定并关闭本轮连接
          setStreaming(false)
          setStatusText('')
          ws.close()
          break

        case 'end':
          // 记录本条回复用时（resume 续播时 sendStartTs 为 null，用时下次刷新由后端 metadata 提供）
          if (sendStartTs !== null) {
            msg.elapsed_seconds = Math.round((Date.now() - sendStartTs) / 1000)
          }
          // 本轮回复结束：解除输入锁定并关闭本轮连接
          setStreaming(false)
          setStatusText('')
         ws.close()
          // 刷新消息列表：让前端拿到 DB id（分支/撤销操作需要 msg.id）
          if (currentSessionIdRef.current) {
            loadMessagesRef.current(currentSessionIdRef.current).catch(() => {})
          }
         // 任务自然结束但还有排队内容没来得及注入：自动作为新任务发送
          if (pendingInputsRef.current.length > 0) {
            sendMergedRef.current?.(pendingInputsRef.current)
          }
          break
      }

      // Update the messages array with the latest streaming message
      setMessages((prev) => {
        const idx = streamingMsgIndexRef.current
        if (idx < 0 || idx >= prev.length) return prev
        const updated = [...prev]
        updated[idx] = { ...msg }
        return updated
      })
      // 事件流滚动：默认跟随最新输出，用户主动上滚后保持当前阅读位置
      scrollToBottom()
    }

    ws.onerror = () => {
      message.error('WebSocket 连接错误')
      // 只处理"当前这轮"连接的错误：end 后自动续发会换新连接，旧连接的迟到回调不能干扰新状态
      if (wsRef.current === ws) {
        setStreaming(false)
        setStatusText('')
      }
    }

    ws.onclose = () => {
      if (wsRef.current === ws) {
        setStreaming(false)
        setStatusText('')
        wsRef.current = null
        streamingMsgRef.current = null
        streamingSessionIdRef.current = null
      }
      scrollToBottom()
    }
  }, [scrollToBottom])

  // 会话有仍在运行的后台任务时（任务在服务端独立运行，切走页面/关闭连接不会中断），
  // 重新连接并以 type=resume 回放事件流，恢复流式显示
  const resumeRunningTask = useCallback(async (sid: number): Promise<boolean> => {
    try {
      const res = await request.get(`/sessions/${sid}/task-status`)
      // 恢复服务端排队内容（刷新/重进会话不丢待发送区）
      const queued: PendingInput[] = (res.data?.queued_inputs || []).map((it: any) => ({
        content: it.content || '',
        files: (it.files || []).map((f: any) => ({
          file_path: f.path || '',
          stored_name: f.stored_name || '',
          original_name: f.name || '',
        })),
      }))
      if (queued.length > 0) setPendingInputs(queued)
      if (!res.data?.running) return false
      if (currentSessionIdRef.current !== sid) return false
      const resumedUsage: TokenUsage | undefined = res.data?.token_usage || undefined
      const resumedRunId: string | undefined = res.data?.run_id || undefined
      if (resumedUsage && resumedRunId) {
        setLiveUsage({ sessionId: sid, runId: resumedRunId, usage: resumedUsage })
      }
      // 任务进行中：补一个流式占位消息，重连续看输出
      const agentMsg: ChatMessage = { role: 'agent', content: '', steps: [], file_outputs: [], token_usage: resumedUsage, run_id: resumedRunId }
      setMessages((prev) => {
        const next = [...prev, agentMsg]
        streamingMsgIndexRef.current = next.length - 1
        return next
      })
      streamingMsgRef.current = agentMsg
      setStreaming(true)
      setStatusText('思考中…')
      scrollToBottom()
      // 从最后一次排队投递之后回放：已落库的段落由历史消息还原，
      // 不重复回放渲染（无投递时 replay_from=0，等价全量回放）
      const replayFrom = Number(res.data?.replay_from || 0)
      const wsUrl = `${getWsBaseUrl()}/api/v1/ws/chat/${sid}`
      const ws = new WebSocket(wsUrl, ['taskpilot-chat', `jwt.${token}`])
      wsRef.current = ws
      streamingSessionIdRef.current = sid
      ws.onopen = () => ws.send(JSON.stringify({ type: 'resume', from_index: replayFrom }))
      attachWsHandlers(ws, null, sid)
      return true
    } catch {
      /* 查询失败按无任务处理，不影响正常使用 */
      return false
    }
  }, [token, attachWsHandlers, scrollToBottom])

  // ===== Effects =====
  useEffect(() => {
    loadSessions()
    loadSkills()
    loadMcpServers()
  }, [loadSessions, loadSkills, loadMcpServers])

  useEffect(() => { void loadAgentInfo() }, [loadAgentInfo])

  useEffect(() => {
    const refresh = () => { if (document.visibilityState === 'visible') { void loadAgentInfo(); void loadMcpServers(); void loadSkills() } }
    window.addEventListener('focus', refresh)
    document.addEventListener('visibilitychange', refresh)
    return () => { window.removeEventListener('focus', refresh); document.removeEventListener('visibilitychange', refresh) }
  }, [loadAgentInfo, loadMcpServers, loadSkills])

  // Agent switched or its MCP binding changed: drop selections that are no longer bound
  useEffect(() => {
    const current = useAuthStore.getState().selectedMcps
    if (current === null) return
    const valid = current.filter((id) => boundMcpIds.includes(id))
    if (valid.length !== current.length) {
      setSelectedMcps(valid.length === boundMcpIds.length ? null : valid)
    }
  }, [boundMcpIds, setSelectedMcps])

  // 切换 Agent 时清空当前会话：历史记录按 Agent 隔离，由 loadSessions 重新挑本 Agent 的会话
  useEffect(() => {
    setCurrentSessionId(null)
    currentSessionIdRef.current = null
    prevSessionIdRef.current = null
    setSessions([])
    setMessages([])
    setVisibleMessagesSessionId(null)
    setStoredUsage(null)
    setLiveUsage(null)
  }, [selectedAgentId])

  // 跨 Agent 跳转：URL 带 ?agent=X（如从文件中心"来源"跳转）时先切到目标 Agent，
  // 否则会按当前 Agent 过滤会话列表，找不到目标会话导致跳转失效
 const routeAgentId = searchParams.get('agent')
  const routePanel = searchParams.get('panel')
 useEffect(() => {
   const id = routeAgentId ? parseInt(routeAgentId, 10) : NaN
   if (!Number.isNaN(id) && id !== selectedAgentId) {
     setSelectedAgentId(id)
   }
 }, [routeAgentId, setSelectedAgentId])

  useEffect(() => {
    if (routePanel === 'plan' || routePanel === 'mcp' || routePanel === 'skill' || routePanel === 'nav') {
      setActivePanel(routePanel)
    }
  }, [routePanel])

 useEffect(() => {
    if (currentSessionId && currentSessionId !== prevSessionIdRef.current) {
      prevSessionIdRef.current = currentSessionId
      setLiveUsage((prev) => prev?.sessionId === currentSessionId ? prev : null)
      const sendingHere = streamingSessionIdRef.current === currentSessionId && wsRef.current !== null
      // 离开旧会话：关闭当前 WebSocket 订阅（服务端后台任务不受影响，继续跑），
      // 并复位流式状态，避免旧会话的输出 chunk 写进新会话的消息列表
      if (wsRef.current && !sendingHere) {
        wsRef.current.close()
        wsRef.current = null
      }
      if (!sendingHere) {
        setStreaming(false)
        streamingMsgRef.current = null
        streamingMsgIndexRef.current = -1
      }
      // 还原本会话的输入草稿（文本/附件/模型）：草稿按会话独立、持久化存储，切走不丢
      const draft = useDraftStore.getState().drafts[String(currentSessionId)]
      skipDraftSaveRef.current += 1
      setInputValue(draft?.text || '')
      setUploadedFiles(((draft?.files as UploadedFile[] | undefined) || []))
      setModel(draft?.model || '')
      setPendingInputs(((draft?.pending as PendingInput[] | undefined) || []))
      if (!sendingHere) (async () => {
        await loadMessages(currentSessionId)
        // 后台还有任务在跑则重连续看（切出去再回来也能看到进行中的输出）
        const running = await resumeRunningTask(currentSessionId)
        // 任务没在跑但仍有排队内容（如离线期间任务已结束）：自动作为新任务发送
        if (!running && pendingInputsRef.current.length > 0) {
          sendMergedRef.current?.(pendingInputsRef.current)
        }
      })()
      // Update URL
      if (routeSessionId !== String(currentSessionId)) {
        navigate(`/chat/${currentSessionId}`, { replace: true })
      }
    }
  }, [currentSessionId, loadMessages, navigate, routeSessionId, resumeRunningTask])

  // 草稿持久化：输入内容/附件/模型变化即按会话保存；全部为空时清掉草稿
  useEffect(() => {
    if (!currentSessionId) return
    if (skipDraftSaveRef.current > 0) {
      skipDraftSaveRef.current -= 1
      return
    }
    const empty = !inputValue.trim() && uploadedFiles.length === 0 && !model && pendingInputs.length === 0
    if (empty) {
      useDraftStore.getState().clearDraft(currentSessionId)
    } else {
      useDraftStore.getState().saveDraft(currentSessionId, {
        text: inputValue,
        files: uploadedFiles,
        model,
        pending: pendingInputs,
      })
    }
  }, [currentSessionId, inputValue, uploadedFiles, model, pendingInputs])

  // Cleanup WebSocket on unmount
  useEffect(() => {
    return () => {
      if (wsRef.current) {
        wsRef.current.close()
        wsRef.current = null
      }
    }
  }, [])

  // ===== Create session =====
 const createSession = async () => {
    if (!selectedAgentId) {
      message.warning('请先选择一个 Agent')
      return null
    }
    try {
      const res = await request.post('/sessions/', {
        agent_id: selectedAgentId,
        title: '新对话',
      })
      const newSession: Session = res.data
      setSessions((prev) => [newSession, ...prev])
      setMessages([])
      setVisibleMessagesSessionId(newSession.id)
      setLiveUsage(null)
      setStoredUsage({ sessionId: newSession.id, usage: null })
      setCurrentSessionId(newSession.id)
      return newSession
    } catch (err) {
      message.error('创建会话失败')
      return null
    }
  }

  // ===== Delete session =====
  const deleteSession = async (sid: number, e: React.MouseEvent) => {
    e.stopPropagation()
    Modal.confirm({
      title: '删除此会话？',
      okText: '删除',
      okButtonProps: { danger: true },
      onOk: async () => {
        try {
          await request.delete(`/sessions/${sid}`)
          setSessions((prev) => prev.filter((s) => s.id !== sid))
          if (currentSessionId === sid) {
            setCurrentSessionId(null)
            setMessages([])
            navigate('/chat', { replace: true })
          }
        } catch {
          message.error('删除会话失败')
        }
      },
    })
  }

  // ===== Rename session =====
  const startRename = (sid: number, title: string, e: React.MouseEvent) => {
    e.stopPropagation()
    setEditingSessionId(sid)
    setEditingTitle(title)
  }

  const confirmRename = async (sid: number) => {
    if (!editingTitle.trim()) return
    try {
      await request.patch(`/sessions/${sid}/title?title=${encodeURIComponent(editingTitle.trim())}`)
      setSessions((prev) =>
        prev.map((s) => (s.id === sid ? { ...s, title: editingTitle.trim() } : s))
      )
      setEditingSessionId(null)
    } catch {
      message.error('重命名失败')
    }
  }

  // ===== Handle file upload =====
  const handleUpload = async (file: File) => {
    if (!file.size || file.size > 50 * 1024 * 1024) {
      message.error(`${file.name || '附件'}：文件为空或超过 50 MB`)
      return false
    }
    const targetAgent = selectedAgentRef.current
    const targetSession = currentSessionIdRef.current
    const formData = new FormData()
    formData.append('file', file)
    setUploadingCount((count) => count + 1)
    try {
      const res = await request.post('/upload/', formData, {
        timeout: 120000,
      })
      const filePath = res.data?.file_path
      if (filePath) {
        if (selectedAgentRef.current !== targetAgent || currentSessionIdRef.current !== targetSession) return false
        setUploadedFiles((prev) => [
          ...prev,
          {
            file_path: filePath,
            stored_name: res.data?.stored_name || file.name,
            original_name: res.data?.original_name || file.name,
            size: res.data?.size,
          },
        ])
        message.success(`已上传：${file.name}`)
      }
    } catch (error: any) {
      message.error(`${file.name} 上传失败：${error.response?.data?.detail || error.message || '请重试'}`)
    } finally {
      setUploadingCount((count) => Math.max(0, count - 1))
    }
    return false // prevent antd auto upload
  }

  const compressContext = async () => {
    const sessionId = currentSessionIdRef.current
    if (!sessionId || streaming || compressingSessionId === sessionId) return
    setCompressingSessionId(sessionId)
    try {
      const { data } = await request.post(`/sessions/${sessionId}/compress`, null, { timeout: 180000 })
      if (currentSessionIdRef.current === sessionId) {
        if (data.status === 'completed') await loadMessages(sessionId)
        else message.info(data.detail || '无需压缩')
      }
    } catch (error: any) {
      if (currentSessionIdRef.current === sessionId) {
        message.error(error.response?.data?.detail || '压缩失败，原上下文保持不变')
      }
    } finally {
      setCompressingSessionId((current) => current === sessionId ? null : current)
    }
  }

  // ===== Handle file download =====
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
  // 下载 Agent 生成的输出文件
  const handleDownload = (filename: string) =>
    downloadFile(`/api/v1/upload/download/${encodeURIComponent(filename)}`, filename)
  // 下载用户自己上传的附件
  const handleDownloadUploaded = (storedName: string, originalName: string) =>
    downloadFile(`/api/v1/upload/download-upload/${encodeURIComponent(storedName)}`, originalName)

  // ===== Send message via WebSocket =====
  // 附件序列化：后端期望键名 path/name/stored_name
  const serializeFiles = (list: UploadedFile[]) =>
    list.map((f) => ({ path: f.file_path, name: f.original_name, stored_name: f.stored_name }))

  // 把若干输入项合并为一条消息并作为新任务发送（无运行中任务时使用：
  // 普通发送 / 排队被拒回退 / 任务自然结束后的自动续发）
  const sendMerged = (items: PendingInput[], sessionIdOverride?: number) => {
    const valid = items.filter((it) => it.content.trim() || it.files.length > 0)
    if (valid.length === 0) return
    const sid = sessionIdOverride ?? currentSessionId
    if (!sid) {
      message.warning('请先创建或选择一个会话')
      return
    }
    setVisibleMessagesSessionId(sid)
    const text = valid.map((it) => it.content.trim()).filter(Boolean).join('\n\n')
    const allFiles = valid.flatMap((it) => it.files)

    // Add user message + streaming placeholder to UI（附件立即显示在用户消息里）
    const userMsg: ChatMessage = {
      role: 'user',
      content: text,
      attachments: allFiles.map((f) => ({ name: f.original_name, stored_name: f.stored_name })),
      created_at: new Date().toISOString(),
    }
    const agentMsg: ChatMessage = { role: 'agent', content: '', steps: [], file_outputs: [] }

    setMessages((prev) => {
      const next = [...prev, userMsg, agentMsg]
      streamingMsgIndexRef.current = next.length - 1
      return next
    })

    setInputValue('')
    setUploadedFiles([])
    setPendingInputs([])
    setStreaming(true)
    setStatusText('思考中…')
    scrollToBottom(true)

    // Initialize streaming message
    streamingMsgRef.current = { role: 'agent', content: '', steps: [], file_outputs: [] }
    const sendStartTs = Date.now()  // 本地计时：回复结束时显示"用时xx"

    // Connect WebSocket
    const wsUrl = `${getWsBaseUrl()}/api/v1/ws/chat/${sid}`
    const ws = new WebSocket(wsUrl, ['taskpilot-chat', `jwt.${token}`])
    wsRef.current = ws
    streamingSessionIdRef.current = sid

    ws.onopen = () => {
      const payload = {
        content: text,
        // 始终发送 selected_skills：空数组 = 不加载任何可选技能（内置技能不受影响，始终按需可用）
       selected_skills: selectedSkills,
       // null = 不限制，加载 Agent 绑定的全部 MCP（模型自主选用）；数组 = 仅加载勾选的子集
       selected_mcps: selectedMcps,
       // 用户所选模型（不传=用 Agent 的默认模型）
        ...(model ? { model } : {}),
        files: serializeFiles(allFiles),
      }
      ws.send(JSON.stringify(payload))
    }

    attachWsHandlers(ws, sendStartTs, sid)
  }
  // ws 事件回调（attachWsHandlers）里引用 sendMerged 要走 ref，避免闭包过期
 const sendMergedRef = useRef<(items: PendingInput[], sessionIdOverride?: number) => void>(() => {})
 useEffect(() => { sendMergedRef.current = sendMerged })
  // end 事件后刷新消息列表（让前端拿到 DB id，分支/撤销操作依赖 msg.id）
  const loadMessagesRef = useRef(loadMessages)
  useEffect(() => { loadMessagesRef.current = loadMessages })

  // 发送按钮 / Enter：无任务=正常发送；任务运行中=排队（延时发送，挂在输入框待发送区）
  const sendMessage = async () => {
    if (runtimeLoading || runtimeError) { message.warning('请先等待会话配置加载完成'); return }
    if (compressingSessionId !== null && compressingSessionId === currentSessionIdRef.current) return
    if (uploadingCount > 0) {
      message.warning('附件上传中，请稍候发送')
      return
    }
    const text = inputValue.trim()
    const hasComposer = !!text || uploadedFiles.length > 0
    if (!hasComposer && pendingInputs.length === 0) return
    let sessionId = currentSessionId
    if (!sessionId) {
      const newSession = await createSession()
      if (!newSession) return
      sessionId = newSession.id
    }

    if (streaming) {
      // 排队：把当前完整队列快照发给后端整体替换；任务下一轮模型调用时注入给模型
      const ws = wsRef.current
      if (!ws || ws.readyState !== WebSocket.OPEN) {
        message.error('连接不可用，请稍候重试')
        return
      }
      const items: PendingInput[] = hasComposer
        ? [...pendingInputs, { content: text, files: uploadedFiles }]
        : [...pendingInputs]
      ws.send(JSON.stringify({
        type: 'queue_input',
        items: items.map((it) => ({ content: it.content, files: serializeFiles(it.files) })),
      }))
      setPendingInputs(items)
      if (hasComposer) {
        setInputValue('')
        setUploadedFiles([])
      }
      return
    }

    sendMerged([...pendingInputs, ...(hasComposer ? [{ content: text, files: uploadedFiles }] : [])], sessionId)
  }

  // 移除一条待发送项：运行中同步全量快照到后端队列（替换语义）
  const removePendingItem = (index: number) => {
    const next = pendingInputs.filter((_, idx) => idx !== index)
    setPendingInputs(next)
    if (streaming) {
      const ws = wsRef.current
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({
          type: 'queue_input',
          items: next.map((it) => ({ content: it.content, files: serializeFiles(it.files) })),
        }))
      }
    }
  }

  // Esc：停止当前对话并把排队内容立即上传给模型。
  // 已排队项此前已随 queue_input 快照同步到服务端，这里只补发输入框里尚未排队的草稿；
  // 后端停掉旧任务后把"服务端队列 + 草稿"合并为新任务立即发出（stop_ack 回执）
  const handleStop = () => {
    // 立即切换 UI 状态，不等 stop_ack 才清——用户按 Esc 时体感"卡住"的根因
    // 就是后端清理流程（producer 取消 + AgentScope 优雅收尾）最长 15 秒才回 stop_ack
    setStreaming(false)
    const ws = wsRef.current
    if (!ws || ws.readyState !== WebSocket.OPEN) return
    const text = inputValue.trim()
    const draftItems = (text || uploadedFiles.length > 0)
      ? [{ content: text, files: serializeFiles(uploadedFiles) }]
      : []
    ws.send(JSON.stringify({
      type: 'stop',
      items: draftItems,
      ...(model ? { model } : {}),
    selected_skills: selectedSkills,
    selected_mcps: selectedMcps,
   }))
  }
  const handleStopRef = useRef(() => {})
  useEffect(() => { handleStopRef.current = handleStop })

  // Esc 监听只在流式输出期间生效
  useEffect(() => {
    if (!streaming) return
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault()
        handleStopRef.current()
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
 }, [streaming])

  // ===== 用户消息气泡操作：分支 / 撤销 =====
  const handleBranch = async (msg: ChatMessage) => {
    if (!currentSessionId || !msg.id) return
    try {
      const res = await request.post(`/sessions/${currentSessionId}/branch?from_message_id=${msg.id}`)
      const newSession: Session = res.data
      setSessions((prev) => [newSession, ...prev])
      setCurrentSessionId(newSession.id)
      message.success('已创建分支会话')
    } catch (e: any) {
      message.error(e.response?.data?.detail || '分支创建失败')
    }
  }

  const handleUndo = async (msg: ChatMessage) => {
    if (!currentSessionId || !msg.id || streaming) return
    try {
      const res = await request.delete(`/sessions/${currentSessionId}/messages/${msg.id}`)
      setInputValue(res.data.content || '')
      const attachments = res.data.attachments || []
      if (attachments.length > 0) {
        setUploadedFiles(attachments.map((a: any) => ({
          file_path: '',
          stored_name: a.stored_name,
          original_name: a.name,
        })))
      }
      await loadMessages(currentSessionId)
      message.success('已撤销，内容已回到输入框')
    } catch (e: any) {
      message.error(e.response?.data?.detail || '撤销失败')
    }
  }

  // ===== ask_user：用户选择方案 / 填写自定义方案 / 跳过 =====
  const sendAskUserReply = (requestId: string, answerText: string) => {
    const ws = wsRef.current
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      message.error('连接不可用，请稍后重试')
      return
    }
    ws.send(JSON.stringify({ type: 'external_reply', tool_call_id: requestId, answer_text: answerText }))
  }

  const sendAskUserCancel = (requestId: string) => {
    const ws = wsRef.current
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      message.error('连接不可用，请稍后重试')
      return
    }
    ws.send(JSON.stringify({ type: 'external_reply', tool_call_id: requestId, skipped: true }))
  }

  // ===== Handle Enter key =====
  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      sendMessage()
    }
  }

  // ===== Skill toggle =====
  const toggleSkill = (name: string) => {
    const next = selectedSkills.includes(name)
      ? selectedSkills.filter((s) => s !== name)
      : [...selectedSkills, name]
    setSelectedSkills(next)
  }
  // 包级批量全选/全不选：读 store 最新状态一次性算好目标数组，单次更新原子生效
  // （逐个 toggle 会因渲染快照过期互相覆盖，必须走这里）
  const batchToggleSkills = (names: string[], select: boolean) => {
    const current = useAuthStore.getState().selectedSkills
    const nameSet = new Set(names)
    const next = select
      ? Array.from(new Set([...current, ...names]))
      : current.filter((s) => !nameSet.has(s))
    setSelectedSkills(next)
  }

  // ===== Render: Session List =====
  const renderSessionList = () => (
    <div className="session-list">
      {sessions.map((s) => (
        <div
          key={s.id}
          className={`session-item ${currentSessionId === s.id ? 'active' : ''}`}
          onClick={() => setCurrentSessionId(s.id)}
        >
          {editingSessionId === s.id ? (
            <Input
              size="small"
              value={editingTitle}
              onChange={(e) => setEditingTitle(e.target.value)}
              onBlur={() => confirmRename(s.id)}
              onPressEnter={() => confirmRename(s.id)}
              autoFocus
              style={{ flex: 1 }}
            />
          ) : (
            <>
              <span className="session-item-title">{s.title}</span>
             <span className="session-item-actions">
                <Tooltip title="重命名">
                 <Button
                   type="text"
                   size="small"
                   icon={<EditOutlined />}
                   onClick={(e) => startRename(s.id, s.title, e)}
                 />
               </Tooltip>
                <Tooltip title="删除">
                  <Button
                    type="text"
                    size="small"
                    danger
                    icon={<DeleteOutlined />}
                    onClick={(e) => deleteSession(s.id, e)}
                  />
                </Tooltip>
              </span>
            </>
          )}
        </div>
      ))}
    </div>
  )

  // ===== Render: Messages =====
  // Note: the container uses flex-direction: column-reverse, so the array is
  // rendered in reverse. forwardIndex always refers to the position in the
  // original (chronological) messages array and is used for anchor ids.
  const renderMessages = () => {
    if (loading || (currentSessionId !== null && visibleMessagesSessionId !== currentSessionId)) {
      return (
        <div style={{ display: 'flex', justifyContent: 'center', padding: 40 }}>
          <Spin size="large" />
        </div>
      )
    }
    if (messages.length === 0) {
      return (
        <div
          style={{
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'center',
            justifyContent: 'center',
            height: '100%',
            color: 'var(--text-tertiary)',
            gap: 12,
          }}
        >
          <RobotOutlined style={{ fontSize: 48, opacity: 0.3 }} />
          <span>开始对话...</span>
        </div>
      )
    }
    return [...messages].reverse().map((msg, idx) => {
      const forwardIndex = messages.length - 1 - idx
      const isStreamingMsg = streaming && forwardIndex === messages.length - 1
      return (
        <div key={msg.id ?? `live-${forwardIndex}`} id={`message-${forwardIndex}`}>
          <MessageItem
            msg={msg}
            isStreaming={isStreamingMsg}
            displayName={msg.role === 'user' ? user?.username || '你' : agentName}
            onDownload={handleDownload}
            onDownloadUpload={handleDownloadUploaded}
            onCitationClick={onCitationClick}
            onBranch={handleBranch}
            onUndo={handleUndo}
          />
        </div>
      )
    })
  }

  // ===== Right drawer: menu entries, derived MCP list, titles =====
const onCitationClick = useCallback(async (docId: number, chunkIndex: number) => {
    setCitationDrawer({ loading: true, docId, chunkIndex })
    try {
      const r = await getChunk(docId, chunkIndex)
      setCitationDrawer({ loading: false, docId, chunkIndex, content: r.content, filename: r.filename, title: r.title, totalChunks: r.total_chunks })
    } catch {
      setCitationDrawer({ loading: false, docId, chunkIndex })
      message.error('Failed to load chunk')
    }
  }, [])

  const boundMcps = mcpServers.filter((s) => boundMcpIds.includes(s.id) && s.enabled && !s.is_deleted)
  const selectedMcpCount = selectedMcps === null
    ? boundMcps.length
    : selectedMcps.filter((id) => boundMcps.some((s) => s.id === id)).length

  const panelMenuItems = [
    { key: 'plan', icon: <ScheduleOutlined />, label: '计划' },
    {
      key: 'mcp',
      icon: <ApiOutlined />,
      label: boundMcps.length > 0 ? `MCP 服务（${selectedMcpCount}/${boundMcps.length}）` : 'MCP 服务',
    },
    { key: 'skill', icon: <BulbOutlined />, label: `技能（${selectedSkills.length}/${skills.length}）` },
    { key: 'nav', icon: <MenuOutlined />, label: '消息导航' },
  ]

  // Tabs inside the panel: switch without closing, so e.g. skills + MCP can be picked together
  const panelTabs: { key: 'plan' | 'mcp' | 'skill' | 'nav'; icon: React.ReactNode; label: string }[] = [
    { key: 'plan', icon: <ScheduleOutlined />, label: '计划' },
    { key: 'mcp', icon: <ApiOutlined />, label: 'MCP' },
    { key: 'skill', icon: <BulbOutlined />, label: '技能' },
    { key: 'nav', icon: <MenuOutlined />, label: '导航' },
  ]

  // Trigger badge: user has configured skills or an explicit MCP subset
  const hasActiveSelection = selectedSkills.length > 0 || selectedMcps !== null

  const panelTitles: Record<'plan' | 'mcp' | 'skill' | 'nav', string> = {
    plan: '计划',
    mcp: 'MCP 服务',
    skill: '技能',
    nav: '消息导航',
  }

  // ===== Main Render =====
  // 待决策的 ask_user：提升到输入框正上方的决策条（与输入框等宽），
  // 点击后走现有 WS 回传，Agent 继续推理；消息流内只保留记录态提示
  const barAsks = messages.flatMap((m) =>
    (m.steps || []).filter((s) => s.type === 'external_ask' && !s.answer && !s.resolved_by),
  )

  return (
    <>
    <div className="chat-layout">
      {/* ===== Sidebar ===== */}
      <div className={`chat-sider ${siderCollapsed ? 'collapsed' : ''}`}>
        <div className="chat-sider-header">
          <div className="chat-sider-header-left">
            <Dropdown
              trigger={['click']}
              menu={{ items: availableAgents.map((agent) => ({
                key: agent.id, label: agent.name, disabled: agent.id === selectedAgentId,
              })), onClick: ({ key }) => switchAgent(Number(key)) }}
            >
              <Button type="text" className="chat-agent-name" title="切换 Agent">
                {agentName} <span aria-hidden="true">▾</span>
              </Button>
            </Dropdown>
          </div>
          <Button className="new-chat-btn" icon={<PlusOutlined />} size="small" onClick={createSession}>
            新建会话
          </Button>
        </div>
        {renderSessionList()}
      </div>

      {/* ===== Main Area ===== */}
      <div className="chat-main">
        <div className="chat-content">
        {/* Header */}
        <div className="chat-header">
          <div className="chat-header-left">
            <Button
              type="text"
              icon={<MenuOutlined />}
              onClick={() => setSiderCollapsed(!siderCollapsed)}
            />
            <span className="chat-header-title">
              {currentSessionId
                ? sessions.find((s) => s.id === currentSessionId)?.title || '聊天'
                : '选择会话'}
            </span>
          </div>
          <div className="chat-header-actions">
            {streaming && <LoadingOutlined spin />}
            <Dropdown
              menu={{
                items: panelMenuItems,
                onClick: ({ key }) => setActivePanel(key as 'plan' | 'mcp' | 'skill' | 'nav'),
              }}
              trigger={['click']}
            >
              <Badge dot={hasActiveSelection} offset={[-3, 3]}>
                <Button type="text" className="panel-trigger" icon={<AppstoreOutlined />} title="功能面板" />
              </Badge>
            </Dropdown>
          </div>
        </div>

        {runtimeError && <Alert type="error" showIcon message={runtimeError} action={<Button size="small" onClick={() => void loadAgentInfo()}>重试</Button>} />}
        {usingPreviousConfig && <Alert type="info" showIcon message={`本会话使用历史配置，默认模型：${defaultModel}`} description="助手的新配置已应用；新建会话使用最新配置，当前历史会话保留原配置。" />}
        {/* Messages */}
        <div className="chat-messages-wrapper">
          <div className="chat-messages" ref={chatScrollRef}>
            {renderMessages()}
            <div ref={messagesEndRef} />
          </div>
          {/* 右下角滚动圆钮：锚在消息区内，右侧面板展开时自动跟着收窄后的右下角 */}
          <ScrollEdgeButton
            targetRef={chatScrollRef}
            refreshKey={`${messages.length}:${streaming}:${activePanel ?? ''}`}
            onScrollToTop={scrollToTop}
            onScrollToBottom={() => scrollToBottom(true)}
          />
        </div>

        {/* 进行中指示：思考/工具执行的停顿期显示转圈圆环 + 阶段文案，
            明确告知任务仍在进行，而不是看起来像卡死 */}
          {(streaming || (currentSessionId !== null && compressingSessionId === currentSessionId)) && (
            <div className="chat-progress-pill" role="status">
            <ProgressRing />
            <span>{currentSessionId !== null && compressingSessionId === currentSessionId ? '压缩中' : statusText || '思考中…'}</span>
          </div>
        )}

        {/* Input Area */}
        <div className="chat-input-area">
          {barAsks.map((s, i) => (
            s.kind === 'intake' ? (
              <RequirementsForm
                key={`bar-${s.request_id || i}`}
                step={s}
                onReply={sendAskUserReply}
                onCancel={sendAskUserCancel}
              />
            ) : (
              <AskUserBar
                key={`bar-${s.request_id || i}`}
                step={s}
                onReply={sendAskUserReply}
                onCancel={sendAskUserCancel}
              />
            )
          ))}
          <ChatComposer
            value={inputValue}
            onChange={setInputValue}
           onSend={sendMessage}
            onStop={handleStop}
           onEnter={handleKeyDown}
            sending={streaming}
            disabled={runtimeLoading || !!runtimeError || currentSessionId !== null && compressingSessionId === currentSessionId}
            uploading={uploadingCount > 0}
            files={uploadedFiles}
            onRemoveFile={(i) => setUploadedFiles((prev) => prev.filter((_, idx) => idx !== i))}
            onOpenFile={(f) => handleDownloadUploaded(f.stored_name, f.original_name)}
            onUpload={handleUpload}
           models={models}
           model={model}
           defaultModel={defaultModel}
           onModelChange={setModel}
            pending={pendingInputs}
            onRemovePending={removePendingItem}
          />
          <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, padding: '0 4px' }}>
            <CacheIndicator
              usage={sessionUsage}
            />
            <button
              type="button"
              className="cache-indicator context-compress-btn"
              title="摘要旧对话，保留最近上下文和完整的可查看历史"
              disabled={!currentSessionId || streaming || compressingSessionId === currentSessionId}
              onClick={compressContext}
            >
              <span className="cache-dot" />压缩上下文
            </button>
          </div>
        </div>
        </div>

        {/* Inline side panel: pushes chat content left; tabs switch content without closing.
            All four contents stay mounted so search/selection state survives tab switches. */}
        <div className={`chat-side-panel${activePanel ? ' open' : ''}`}>
          <div className="chat-side-panel-inner">
            <div className="chat-side-panel-head">
              <span className="chat-side-panel-title">{activePanel ? panelTitles[activePanel] : ''}</span>
              <Button
                type="text"
                shape="circle"
                size="small"
                icon={<CloseOutlined />}
                onClick={() => setActivePanel(null)}
                title="关闭面板"
              />
            </div>
            <div className="chat-side-panel-tabs">
              {panelTabs.map((t) => (
                <div
                  key={t.key}
                  className={`chat-side-panel-tab${activePanel === t.key ? ' active' : ''}`}
                  onClick={() => setActivePanel(t.key)}
                >
                  {t.icon}
                  <span>{t.label}</span>
                </div>
              ))}
            </div>
            <div className="chat-side-panel-body">
              <div style={{ display: activePanel === 'plan' ? 'flex' : 'none', flexDirection: 'column', flex: 1, minHeight: 0 }}>
                <PlanPanel messages={messages} />
              </div>
              <div style={{ display: activePanel === 'mcp' ? 'flex' : 'none', flexDirection: 'column', flex: 1, minHeight: 0 }}>
                <McpPickerPanel servers={boundMcps} selectedMcps={selectedMcps} onChange={setSelectedMcps} />
              </div>
              <div style={{ display: activePanel === 'skill' ? 'flex' : 'none', flexDirection: 'column', flex: 1, minHeight: 0 }}>
                <SkillEvolutionPanel compact sessionId={currentSessionId || undefined} onPublished={() => void refreshPersonalSkills()} />
                {personalBindings.length > 0 && <Alert type="info" showIcon message="本会话已关联个人进化版" description="选中来源技能时，后续轮次自动使用个人版。其他会话和未选中的技能不受影响。" />}
                <SkillPanel skills={skills} selectedSkills={selectedSkills.map(name => personalBindings.find(binding => binding.source === name)?.target || name)} onToggle={name => { const binding = personalBindings.find(item => item.target === name); toggleSkill(binding && selectedSkills.includes(binding.source) ? binding.source : name) }} onBatchToggle={(names, select) => batchToggleSkills(names.map(name => { const binding = personalBindings.find(item => item.target === name); return binding && selectedSkills.includes(binding.source) ? binding.source : name }), select)} />
              </div>
              <div style={{ display: activePanel === 'nav' ? 'flex' : 'none', flexDirection: 'column', flex: 1, minHeight: 0 }}>
                <MessageNavPanel messages={messages} />
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
      <Drawer
        open={!!citationDrawer}
        onClose={() => setCitationDrawer(null)}
        width={520}
        title={citationDrawer?.filename || 'Source chunk'}
        loading={citationDrawer?.loading}
      >
        {citationDrawer?.content != null && (
          <div>
            <div style={{ marginBottom: 8, color: '#888', fontSize: 12 }}>
              {citationDrawer.title} | chunk {citationDrawer.chunkIndex} / {citationDrawer.totalChunks}
            </div>
            <div style={{ whiteSpace: 'pre-wrap', fontSize: 14, lineHeight: 1.7, maxHeight: '70vh', overflowY: 'auto' }}>
              {citationDrawer.content}
            </div>
          </div>
        )}
      </Drawer>
    </>
  )
}

export default Chat
