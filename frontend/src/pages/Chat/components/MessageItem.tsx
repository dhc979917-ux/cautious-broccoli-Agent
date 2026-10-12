import { useState, useLayoutEffect, useRef } from 'react'
import { Button, Tag, Tooltip, message } from 'antd'
import {
  UserOutlined,
  RobotOutlined,
  LoadingOutlined,
  ToolOutlined,
  CheckCircleOutlined,
  BulbOutlined,
  DownloadOutlined,
  PaperClipOutlined,
  RightOutlined,
  DownOutlined,
  QuestionCircleOutlined,
  FormOutlined,
  GlobalOutlined,
  CopyOutlined,
  ReloadOutlined,
  PoweroffOutlined,
  ClockCircleOutlined,
  BranchesOutlined,
  UndoOutlined,
} from '@ant-design/icons'
import { ChatMessage, MessageStep, FileOutput, Attachment, ServicePreview } from '../types'
import { formatFileSize } from '../utils'
import { refreshPreview, stopPreview } from '../../../api/preview'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { useAuthStore } from '../../../store/authStore'
import { useCollapseStore } from '../../../store/collapseStore'
import TokenUsageTooltip from '../../../components/TokenUsageTooltip'
import { formatTokenCount, getUncachedTokenTotal } from '../../../utils/tokenUsage'

interface MessageItemProps {
  msg: ChatMessage
  isStreaming: boolean
  displayName: string
  // 只读预览场景（如文件中心来源浮窗看他人会话）不传，产出/附件的下载入口自动隐藏，
  // 避免渲染出点了就 403 的死链
  onDownload?: (filename: string) => void
  onDownloadUpload?: (storedName: string, originalName: string) => void
  onCitationClick?: (docId: number, chunkIndex: number) => void
  onBranch?: (msg: ChatMessage) => void
  onUndo?: (msg: ChatMessage) => void
}

function formatElapsed(sec: number) {
  if (sec < 60) return `${sec}秒`
  const m = Math.floor(sec / 60)
  const s = sec % 60
  return s > 0 ? `${m}分${s}秒` : `${m}分`
}

// 需求采集表单的答案落库时是 JSON 字符串（{题目id: 值}），直接显示会是一串引号。
// 这里还原成"确认 N 项 + 逐项摘要"，与决策卡的"问/答"两行保持同样的信息密度。
function fmtIntakeAnswer(step: MessageStep): string {
  const questions = step.questions || []
  const byId = new Map(questions.map((q) => [q.id, q]))
  let parsed: Record<string, unknown> = {}
  try {
    const obj = JSON.parse(step.answer || '{}')
    if (obj && typeof obj === 'object') parsed = obj as Record<string, unknown>
  } catch {
    return step.answer || ''
  }
  const parts: string[] = []
  let filled = 0
  for (const [id, raw] of Object.entries(parsed)) {
    const label = byId.get(id)?.label || id
    const val = Array.isArray(raw) ? raw.filter(Boolean).join('、') : String(raw ?? '').trim()
    if (!val) continue
    filled += 1
    parts.push(`${label}=${val}`)
  }
  const assumed = questions.length - filled
  const head = `确认 ${filled} 项${assumed > 0 ? `，${assumed} 项用默认值` : ''}`
  return parts.length ? `${head}（${parts.join('；')}）` : head
}

// 输出内容是否像 markdown（工具输出既有终端纯文本也有 markdown 报告，
// 纯文本走 markdown 渲染会丢失换行，所以先做轻量特征判断）
function looksLikeMarkdown(text: string): boolean {
  if (!text) return false
  return /```|^#{1,6}\s|^\s*[-*+]\s|\*\*[^*\n]+\*\*|\[[^\]\n]+\]\([^)\n]+\)|^\s*\|.+\|/m.test(text)
}

// Parse 引用映射:1=92#0;2=94#0, strip it, convert [1] to clickable links
function addCitationLinks(text: string): string {
  if (!text) return text
  // Self-contained format: [cite:doc_id#chunk_index] — convert to sequential [N](#cite-doc-chunk)
  let citeCounter = 0
  text = text.replace(/\[cite:(\d+)#(\d+)\]/g, (_, docId, chunkIdx) => {
    citeCounter++
    return `[${citeCounter}](#cite-${docId}#${chunkIdx})`
  })
  const mapMatch = text.match(/引用映射:([^\n]+)/)
  const map: Record<string, string> = {}
  if (mapMatch) {
    text = text.replace(mapMatch[0], '').trimEnd()
    mapMatch[1].split(';').forEach((pair: string) => {
      const [num, docChunk] = pair.split('=')
      map[num] = docChunk
    })
  }
  // (?!\() 闃叉㈤噸澶嶅尮閰嶅凡鐢熸垚鐨?[N](#cite-...) 閲岀殑 [N] 鍓嶇紑锛?
  // 鍚﹀垯浼氭妸閿氱偣鍐嶆嫾涓€閬嶏紝椤甸潰涓婂嚭鐜拌８鐨?(#cite-X#Y)
  text = text.replace(/\[(\d+)\](?!\()/g, (match, num) => {
    if (map[num]) return `[${num}](#cite-${map[num]})`
    return match
  })
  // 妯″瀷鏀瑰啓鍙樹綋锛氳８鏁板瓧+瑁搁敋鐐癸紙濡?4(#cite-141#0)锛夆啋鍙偣鍑婚摼鎺?
  text = text.replace(/(?<!\])(\d+)\(#cite-(\d+)#(\d+)\)/g, '[$1](#cite-$2#$3)')
  // 鍏滃簳锛氭竻鎺変笉灞炰簬閾炬帴鐨勫鐣?(#cite-X#Y)锛屽彧淇濈暀寮曠敤缂栧彿
  text = text.replace(/(?<!\])\(#cite-\d+#\d+\)/g, '')
  return text
}

// ReactMarkdown custom link renderer: intercept #cite-* as citation clicks
function makeCitationComponent(onCitationClick?: (docId: number, chunkIndex: number) => void) {
  return ({ href, children }: any) => {
    if (href && href.startsWith('#cite-')) {
      const [docId, chunkIdx] = href.slice(6).split('#')
      return (
        <a
          href="#"
          onClick={(e: any) => {
            e.preventDefault()
            if (onCitationClick) onCitationClick(parseInt(docId), parseInt(chunkIdx || '0'))
          }}
          className="citation-link"
          title={`Source: doc ${docId}, chunk ${chunkIdx}`}
        >
          {children}
        </a>
      )
    }
    return <a href={href} target={href?.startsWith('http') ? '_blank' : undefined} rel="noopener noreferrer">{children}</a>
  }
}

// 思考段：受控 <details>，展开/收起状态持久化（默认展开；手动切换后跨刷新保留）。
// 无 DB id 的流式消息退化为组件内 state（落库有 id 后才开始持久）
function ThinkingStep({ msgId, idx, content, streaming }: {
  msgId?: number
  idx: number
  content: string
  streaming: boolean
}) {
  const key = msgId != null ? `think:${msgId}:${idx}` : ''
  const stored = useCollapseStore((s) => (key ? s.states[key] : undefined))
  const setExpanded = useCollapseStore((s) => s.setExpanded)
  const [local, setLocal] = useState(true)
  const open = key ? (stored ?? true) : local
  const bodyRef = useRef<HTMLDivElement | null>(null)
  const autoFollowRef = useRef(true)
  const programmaticScrollUntilRef = useRef(0)
  const lastBodyScrollTopRef = useRef(0)

  useLayoutEffect(() => {
    const el = bodyRef.current
    if (!el || !streaming || !autoFollowRef.current) return
    programmaticScrollUntilRef.current = Date.now() + 80
    el.scrollTop = el.scrollHeight
  }, [content, streaming])

  const handleBodyScroll = () => {
    const el = bodyRef.current
    if (!el) return
    const current = el.scrollTop
    if (Date.now() < programmaticScrollUntilRef.current) {
      lastBodyScrollTopRef.current = current
      return
    }
    const distanceFromBottom = el.scrollHeight - el.clientHeight - current
    const movedUp = current < lastBodyScrollTopRef.current - 2
    if (movedUp || distanceFromBottom > 8) {
      autoFollowRef.current = false
    } else if (distanceFromBottom <= 8) {
      autoFollowRef.current = true
    }
    lastBodyScrollTopRef.current = current
  }

  return (
    <details
      className="step-thinking-details"
      open={open}
      onToggle={(e) => {
        const next = (e.target as HTMLDetailsElement).open
        if (next !== open) {
          if (key) setExpanded(key, next)
          else setLocal(next)
        }
      }}
    >
      <summary className="step-thinking-summary">
        <BulbOutlined className="step-thinking-icon" />
        <span>{streaming ? '思考中…' : '思考过程'}</span>
      </summary>
      <div
        ref={bodyRef}
        className="step-thinking-body"
        onScroll={handleBodyScroll}
        onWheel={(event) => {
          if (event.deltaY < 0) autoFollowRef.current = false
        }}
      >
        {content}
      </div>
    </details>
  )
}

// 工具输出：受控 <details>（默认收起，手动状态持久化）。
// markdown 特征的内容用 ReactMarkdown 渲染，终端纯文本保持 <pre>
function ToolOutputDetails({ msgId, idx, output }: {
  msgId?: number
  idx: number
  output: string
}) {
  const key = msgId != null ? `tool:${msgId}:${idx}` : ''
  const stored = useCollapseStore((s) => (key ? s.states[key] : undefined))
  const setExpanded = useCollapseStore((s) => s.setExpanded)
  const [local, setLocal] = useState(false)
  const open = key ? (stored ?? false) : local
  return (
    <details
      className="step-tool-output"
      open={open}
      onToggle={(e) => {
        const next = (e.target as HTMLDetailsElement).open
        if (next !== open) {
          if (key) setExpanded(key, next)
          else setLocal(next)
        }
      }}
    >
      <summary>查看输出</summary>
      {looksLikeMarkdown(output) ? (
        <div className="md-body step-tool-md">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{output}</ReactMarkdown>
        </div>
      ) : (
        <pre>{output}</pre>
      )}
    </details>
  )
}

// 工具参数展示解析：新消息的 args 已被后端解析成展示文本（命令/路径，真实换行），
// description 单独字段；历史消息存的是原始 JSON，这里兜底解析出关键字段，
// 让新旧消息展示一致（Write/Edit 不展示大段内容，只显示路径与规模）
function parseToolArgsForDisplay(step: MessageStep): { argsText: string; descText: string } {
  const raw = step.args || ''
  if (step.description || !raw.trimStart().startsWith('{')) {
    return { argsText: raw, descText: step.description || '' }
  }
  try {
    const obj = JSON.parse(raw)
    if (!obj || typeof obj !== 'object') return { argsText: raw, descText: '' }
    if (typeof obj.command === 'string') {
      return { argsText: obj.command, descText: obj.description || '' }
    }
    if (typeof obj.file_path === 'string') {
      if (typeof obj.content === 'string') {
        return { argsText: `${obj.file_path}（写入 ${obj.content.length} 字符）`, descText: '写入文件' }
      }
      if (typeof obj.old_string === 'string') {
        return {
          argsText: `${obj.file_path}（${(obj.old_string || '').length} 字符 → ${(obj.new_string || '').length} 字符）`,
          descText: '编辑文件',
        }
      }
      return { argsText: obj.file_path, descText: '读取文件' }
    }
    if (typeof obj.pattern === 'string') {
      const path = obj.path ? `（目录: ${obj.path}）` : ''
      const isGrep = 'output_mode' in obj || 'case_insensitive' in obj || 'glob' in obj
      return { argsText: obj.pattern + path, descText: isGrep ? '搜索内容' : '查找文件' }
    }
    if (typeof obj.skill === 'string') {
      const restObj: Record<string, unknown> = {}
      Object.keys(obj).forEach((k) => { if (k !== 'skill') restObj[k] = obj[k] })
      return { argsText: Object.keys(restObj).length ? JSON.stringify(restObj, null, 2) : '', descText: '' }
    }
  } catch {
    // 不是合法 JSON 就原样展示
  }
  return { argsText: raw, descText: '' }
}

// 长参数（多行命令）默认收进"查看命令"，展开状态持久化（collapseStore）
function ToolArgsDetails({ msgId, idx, argsText, lines }: {
  msgId?: number
  idx: number
  argsText: string
  lines: number
}) {
  const key = msgId != null ? `args:${msgId}:${idx}` : ''
  const stored = useCollapseStore((s) => (key ? s.states[key] : undefined))
  const setExpanded = useCollapseStore((s) => s.setExpanded)
  const [local, setLocal] = useState(false)
  const open = key ? (stored ?? false) : local
  return (
    <details
      className="step-tool-args"
      open={open}
      onToggle={(e) => {
        const next = (e.target as HTMLDetailsElement).open
        if (next !== open) {
          if (key) setExpanded(key, next)
          else setLocal(next)
        }
      }}
    >
      <summary>查看命令（{lines} 行）</summary>
      <code>{argsText}</code>
    </details>
  )
}

function renderSteps(
  steps: MessageStep[],
  msgId: number | undefined,
  showThinking = true,
  streaming = false,
) {
  if (!steps || steps.length === 0) return null
  // 收起状态：全部隐藏（思考段 + 工具调用）
  if (!showThinking) return null
  const visible = steps
  return (
    <div className="message-steps">
      {visible.map((step, idx) => {
        if (step.type === 'thinking') {
          return (
            <ThinkingStep
              key={idx}
              msgId={msgId}
              idx={idx}
              content={step.content || ''}
              streaming={streaming}
            />
          )
        }
        if (step.type === 'tool_call') {
          const { argsText, descText } = parseToolArgsForDisplay(step)
          const argLines = argsText ? argsText.split('\n').length : 0
          return (
            <div key={idx} className="step-tool-box">
              <div className="step-tool-head">
                {step.status === 'calling' ? (
                  <LoadingOutlined spin />
                ) : (
                  <CheckCircleOutlined style={{ color: '#52c41a' }} />
                )}
                <ToolOutlined />
                <span className="step-tool-name">{step.name}</span>
                {descText && <span className="step-tool-desc" title={descText}>{descText}</span>}
              </div>
              {argsText && (argLines > 8 ? (
                <ToolArgsDetails msgId={msgId} idx={idx} argsText={argsText} lines={argLines} />
              ) : (
                <div className="step-tool-cmd" title={argsText}>
                  <code>{argsText}</code>
                </div>
              ))}
              {step.output != null && step.output !== '' && (
                <ToolOutputDetails msgId={msgId} idx={idx} output={step.output} />
              )}
            </div>
          )
        }
        if (step.type === 'external_ask') {
          // 已解决的决策：思考块内只保留"问/答"两行摘要；待决策卡片在输入框上方渲染
          if (!step.answer && !step.resolved_by) return null
          const isIntake = step.kind === 'intake'
          let resultLine = isIntake ? fmtIntakeAnswer(step) : step.answer || ''
          if (step.resolved_by === 'timeout') {
            resultLine = isIntake ? '超时未填，全部采用默认值（记为假设）' : '超时未响应，AI 自动选择最优方案'
          } else if (step.resolved_by === 'cancel' || step.resolved_by === 'interrupt') {
            resultLine = isIntake ? '已跳过，全部采用默认值（记为假设）' : '已跳过，AI 自行决定'
          }
          return (
            <div key={step.request_id || idx} className="step-ask-summary">
              <div className="step-ask-line">
                {isIntake ? <FormOutlined className="step-ask-icon" /> : <QuestionCircleOutlined className="step-ask-icon" />}
                <span>
                  {isIntake
                    ? `需求采集（${(step.questions || []).length} 项）：${step.question || ''}`
                    : `问：${step.question || ''}`}
                </span>
              </div>
              <div className="step-ask-line answer">答：{resultLine}</div>
            </div>
          )
        }
        return null
      })}
    </div>
  )
}

function renderFileOutputs(files: FileOutput[], onDownload?: (filename: string) => void, onPreview?: (filename: string) => void) {
  if (!files || files.length === 0) return null
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6, marginTop: 8 }}>
      {files.map((f, idx) => (
        <div key={idx} className="file-download-card">
          <DownloadOutlined />
          <button
            type="button"
            className="file-output-path"
            style={{ flex: 1, marginLeft: 8 }}
            title="点击在线预览"
            onClick={() => onPreview?.(f.filename)}
          >
            {f.filename}
          </button>
          <span style={{ color: 'var(--text-tertiary)', fontSize: 12 }}>
            {formatFileSize(f.size)}
          </span>
          {onDownload && (
            <Button type="link" size="small" onClick={() => onDownload(f.filename)}>
              下载
            </Button>
          )}
        </div>
      ))}
    </div>
  )
}

// ==================== 服务预览卡片 ====================
// Agent 在会话沙箱里起的 Web 服务，由平台发布成用户可直接点开的临时地址。
// 卡片自己持有最新状态（刷新/停止后就地更新），不往父组件回调，
// 这样聊天页的状态流不受影响；整块不渲染时（后端未开启预览）零副作用。

const PREVIEW_STATUS_META: Record<string, { text: string; color: string }> = {
  active: { text: '运行中', color: 'success' },
  expired: { text: '已失效', color: 'default' },
  blocked: { text: '无法发布', color: 'warning' },
  error: { text: '发布失败', color: 'error' },
}

function formatCountdown(seconds?: number) {
  const s = Math.max(0, Math.floor(seconds || 0))
  if (s <= 0) return ''
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  if (h > 0) return `${h}小时${m}分`
  if (m > 0) return `${m}分${s % 60}秒`
  return `${s}秒`
}

function ServicePreviewCard({
  preview,
  onDownload,
}: {
  preview: ServicePreview
  onDownload?: (filename: string) => void
}) {
  // override 优先于 props：本页内的刷新/停止结果立即生效；切换会话时组件按 id 重建
  const [override, setOverride] = useState<ServicePreview | null>(null)
  const [busy, setBusy] = useState<'refresh' | 'stop' | null>(null)
  // 倒计时本地走秒，避免依赖父组件重渲染（消息流结束后不会再有事件推动更新）
  const [tick, setTick] = useState(0)
  const view = override || preview
  const isActive = view.status === 'active'

  useLayoutEffect(() => {
    if (!isActive) return
    const timer = window.setInterval(() => setTick((t) => t + 1), 1000)
    return () => window.clearInterval(timer)
  }, [isActive, view.id])

  const left = isActive
    ? Math.max(0, (view.seconds_left || 0) - tick)
    : 0
  const meta = PREVIEW_STATUS_META[view.status] || { text: view.status, color: 'default' }

  const copyUrl = async () => {
    if (!view.url) return
    try {
      await navigator.clipboard.writeText(view.url)
      message.success('预览地址已复制')
    } catch {
      message.warning('浏览器拒绝了剪贴板访问，请手动选中复制')
    }
  }

  const doRefresh = async () => {
    setBusy('refresh')
    try {
      setOverride(await refreshPreview(view.id))
    } catch (e: any) {
      message.error(e?.response?.data?.detail || '刷新预览状态失败')
    } finally {
      setBusy(null)
    }
  }

  const doStop = async () => {
    setBusy('stop')
    try {
      setOverride(await stopPreview(view.id))
      message.success('预览已停止')
    } catch (e: any) {
      message.error(e?.response?.data?.detail || '停止预览失败')
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className={`service-preview-card status-${view.status}`}>
      <div className="sp-head">
        <GlobalOutlined className="sp-icon" />
        <span className="sp-title">服务预览</span>
        <Tag color={meta.color} className="sp-tag">{meta.text}</Tag>
        {isActive && left > 0 && (
          <Tooltip title={`到期时间 ${view.expires_at || '未知'}；沙箱被回收会提前失效`}>
            <span className="sp-ttl">
              <ClockCircleOutlined /> {formatCountdown(left)}
            </span>
          </Tooltip>
        )}
        <span className="sp-spacer" />
        {isActive && (
          <Tooltip title="重新探测沙箱并续期">
            <Button
              size="small"
              type="text"
              icon={<ReloadOutlined spin={busy === 'refresh'} />}
              onClick={doRefresh}
              disabled={busy !== null}
              aria-label="刷新预览状态"
            />
          </Tooltip>
        )}
        {isActive && (
          <Tooltip title="停止预览并释放端口">
            <Button
              size="small"
              type="text"
              danger
              icon={<PoweroffOutlined />}
              onClick={doStop}
              disabled={busy !== null}
              aria-label="停止预览"
            />
          </Tooltip>
        )}
      </div>

      {view.url ? (
        <div className="sp-url-row">
          <a className="sp-url" href={view.url} target="_blank" rel="noreferrer noopener">
            {view.url}
          </a>
          <Tooltip title="复制访问地址">
            <Button
              size="small"
              type="text"
              icon={<CopyOutlined />}
              onClick={copyUrl}
              aria-label="复制预览地址"
            />
          </Tooltip>
        </div>
      ) : (
        <div className="sp-nourl">
          {isActive ? '未配置对外访问基址，只能显示端口' : '预览地址已失效'}
          {view.host_port ? `（端口 ${view.host_port}）` : ''}
        </div>
      )}

      {view.detail && <div className="sp-detail">{view.detail}</div>}

      <div className="sp-meta">
        <span>容器内端口 {view.target_port}</span>
        {view.entry_command && (
          <Tooltip title={view.entry_command}>
            <code className="sp-cmd">{view.entry_command}</code>
          </Tooltip>
        )}
      </div>

      {view.bundle && onDownload && (
        <div className="sp-bundle">
          <Button
            size="small"
            icon={<DownloadOutlined />}
            onClick={() => onDownload(view.bundle!.filename)}
          >
            源码包（含 HOW_TO_RUN.md）
          </Button>
          <span className="sp-bundle-size">{formatFileSize(view.bundle.size)}</span>
        </div>
      )}

      <div className="sp-note">
        预览跑在会话的隔离沙箱里，属临时地址：沙箱回收或到期后立即失效。长期使用请下载源码包，
        按包内 HOW_TO_RUN.md 在自己的环境部署。
      </div>
    </div>
  )
}

function renderServicePreviews(
  previews: ServicePreview[],
  onDownload?: (filename: string) => void,
) {
  if (!previews || previews.length === 0) return null
  return (
    <div className="service-preview-list">
      {previews.map((p) => (
        <ServicePreviewCard key={p.id} preview={p} onDownload={onDownload} />
      ))}
    </div>
  )
}

function renderTokenUsage(msg: ChatMessage) {
  if (!msg.token_usage) return null
  const total = getUncachedTokenTotal(msg.token_usage)
  return (
    <div className="token-usage">
      <Tooltip title={<TokenUsageTooltip usage={msg.token_usage} />} overlayClassName="usage-tooltip-overlay" trigger={['hover', 'focus']}>
        <span className="token-usage-label" tabIndex={0} aria-label={`本次回复非缓存 Tokens ${formatTokenCount(total)}，查看非缓存输入和输出`}>
          Tokens: {formatTokenCount(total)}
        </span>
      </Tooltip>
      {msg.tool_call_count !== undefined && ` | Tools: ${msg.tool_call_count}`}
    </div>
  )
}

function renderAttachments(attachments: Attachment[], onDownloadUpload?: (s: string, o: string) => void) {
  if (!attachments || attachments.length === 0) return null
  return (
    <div className="message-attachments">
      {attachments.map((a, idx) => (
        <div
          key={idx}
          className="attachment-chip"
          onClick={onDownloadUpload ? () => onDownloadUpload(a.stored_name, a.name) : undefined}
          title={onDownloadUpload ? '点击下载' : undefined}
          style={onDownloadUpload ? undefined : { cursor: 'default' }}
        >
          <PaperClipOutlined />
          <span className="attachment-chip-name">{a.name}</span>
        </div>
      ))}
    </div>
  )
}

export default function MessageItem({ msg, isStreaming, displayName, onDownload, onDownloadUpload, onCitationClick, onBranch, onUndo }: MessageItemProps) {
  // 推理过程总开关：默认展开；手动切换的状态持久化到 localStorage（按消息 DB id），
  // 刷新/切会话后保持。流式消息还没有 DB id 时用组件 state 兜底
  const stepsKey = msg.id != null ? `steps:${msg.id}` : ''
  const storedSteps = useCollapseStore((s) => (stepsKey ? s.states[stepsKey] : undefined))
  const setCollapseStore = useCollapseStore((s) => s.setExpanded)
  const [localStepsExpanded, setLocalStepsExpanded] = useState(true)
  const stepsExpanded = stepsKey ? (storedSteps ?? true) : localStepsExpanded
  // 折叠/展开会引起消息高度突变，消息容器是 column-reverse，浏览器滚动锚定
  // 会把视图甩到别的消息上。切换前记录开关按钮的视口位置，渲染后补偿 scrollTop，
  // 让按钮停在原处，视觉上不乱跳
  const toggleRef = useRef<HTMLSpanElement>(null)
  const collapseAnchorY = useRef<number | null>(null)

  const toggleSteps = () => {
    if (toggleRef.current) {
      collapseAnchorY.current = toggleRef.current.getBoundingClientRect().top
    }
    const next = !stepsExpanded
    if (stepsKey) setCollapseStore(stepsKey, next)
    else setLocalStepsExpanded(next)
  }

  useLayoutEffect(() => {
    if (collapseAnchorY.current == null) return
    const targetY = collapseAnchorY.current
    collapseAnchorY.current = null
    const el = toggleRef.current
    if (!el) return
    const delta = el.getBoundingClientRect().top - targetY
    if (delta === 0) return
    const scroller = el.closest('.chat-messages') as HTMLElement | null
    if (scroller) scroller.scrollTop += delta
  }, [stepsExpanded])
  // 用户消息头像：优先使用个人信息里上传的头像
  const userAvatarUrl = useAuthStore((s) => s.user?.avatar_url)
  // Agent 头像：优先显示当前用户设置的 Agent 头像，未设置则保持默认机器人图标
  const agentAvatarUrl = useAuthStore((s) => s.user?.agent_avatar_url)
  // 产出文件在线预览：用 ?token=xxx 直接在新标签页打开（浏览器无法附 Authorization header）
  const token = useAuthStore((s) => s.token)
  const handleFilePreview = (filename: string) => {
    window.open(`/api/v1/upload/preview/${encodeURIComponent(filename)}?token=${token}`, '_blank')
  }
  // 消息时间：只显示时分秒（HH:MM:SS），不带日期
  const timeStr = msg.created_at
    ? new Date(msg.created_at).toLocaleTimeString('zh-CN', { hour12: false })
    : ''
  // 待用户决策的 ask_user 卡片：仅在该消息仍在流式（任务活着）时显示；
  // 任务结束/死亡后不再渲染，避免历史里残留"等待你的决策"的死提示
  const pendingAsks = isStreaming
    ? (msg.steps || []).filter(
        (s) => s.type === 'external_ask' && !s.answer && !s.resolved_by,
      )
    : []
  return (
    <div className={`message-wrapper ${msg.role}`}>
      <div className={`message-avatar ${msg.role}`}>
        {msg.role === 'user'
          ? (userAvatarUrl
              ? <img src={userAvatarUrl} alt="" style={{ width: '100%', height: '100%', borderRadius: '50%', objectFit: 'cover' }} />
              : <UserOutlined />)
          : (agentAvatarUrl
              ? <img src={agentAvatarUrl} alt="" style={{ width: '100%', height: '100%', borderRadius: '50%', objectFit: 'cover' }} />
              : <RobotOutlined />)}
      </div>
      <div className="message-body">
        <div className="message-header">
          {timeStr && <span className="message-time">{timeStr}</span>}
          <span>{displayName}</span>
          {msg.role === 'agent' && (msg.steps?.length ?? 0) > 0 && (
            <span
              ref={toggleRef}
              className="message-elapsed-toggle"
              onClick={toggleSteps}
              title={stepsExpanded ? '收起推理过程' : '展开推理过程'}
            >
              {msg.elapsed_seconds != null
                ? <span>（用时{formatElapsed(msg.elapsed_seconds)}）</span>
                : <span>（推理过程）</span>}
              {stepsExpanded ? <DownOutlined /> : <RightOutlined />}
            </span>
          )}
        </div>
        {msg.role === 'user' && renderAttachments(msg.attachments || [], onDownloadUpload)}
        {msg.role === 'agent' && renderSteps(msg.steps || [], msg.id, stepsExpanded, isStreaming)}
        {msg.role === 'agent' && pendingAsks.map((s, i) => (
          <div key={`pending-${s.request_id || i}`} className="askuser-inline-hint">
            {s.kind === 'intake' ? <FormOutlined /> : <QuestionCircleOutlined />}
            {s.kind === 'intake'
              ? `开工前需要确认几项${s.question ? `：${s.question}` : ''}（表单在输入框上方，留空即采用默认值）`
              : `等待你的决策${s.question ? `：${s.question}` : ''}（选项见输入框上方）`}
          </div>
        ))}
        <div className={`message-bubble ${msg.role}`}>
          {msg.role === 'agent' && msg.content ? (
            <div className="md-body">
              <ReactMarkdown remarkPlugins={[remarkGfm]} components={{ a: makeCitationComponent(onCitationClick) }}>{addCitationLinks(msg.content)}</ReactMarkdown>
            </div>
          ) : (
            msg.content || (isStreaming ? '...' : '')
          )}
        </div>
       {msg.role === 'user' && (
         <div className="user-msg-actions">
            <Button type="text" size="small" title="从此消息创建新会话" icon={<BranchesOutlined />} onClick={() => onBranch?.(msg)} disabled={isStreaming} />
            <Button type="text" size="small" title="撤销此消息" icon={<UndoOutlined />} onClick={() => onUndo?.(msg)} disabled={isStreaming} />
            <Button type="text" size="small" title="复制消息" icon={<CopyOutlined />} onClick={() => {
              try {
                if (navigator.clipboard) {
                  navigator.clipboard.writeText(msg.content).then(() => message.success('已复制')).catch(() => {})
                } else {
                  const ta = document.createElement('textarea')
                  ta.value = msg.content
                  ta.style.position = 'fixed'
                  ta.style.opacity = '0'
                  document.body.appendChild(ta)
                  ta.select()
                  document.execCommand('copy')
                  document.body.removeChild(ta)
                  message.success('已复制')
                }
              } catch { /* ignore */ }
            }} />
         </div>
       )}
        {msg.role === 'agent' && renderFileOutputs(msg.file_outputs || [], onDownload, handleFilePreview)}
        {msg.role === 'agent' && renderServicePreviews(msg.service_previews || [], onDownload)}
        {msg.role === 'agent' && renderTokenUsage(msg)}
      </div>
    </div>
  )
}
