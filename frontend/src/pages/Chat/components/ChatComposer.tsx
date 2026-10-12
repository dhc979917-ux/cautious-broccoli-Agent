import { useRef, useState } from 'react'
import { Button, Dropdown, Input, Tooltip, message } from 'antd'
import { PlusOutlined, ArrowUpOutlined, HourglassOutlined, CloseOutlined, StopOutlined, ExpandAltOutlined, CompressOutlined } from '@ant-design/icons'
import type { UploadedFile, PendingInput } from '../types'
import { useModelDirectory } from '../../../store/modelDirectory'

interface ChatComposerProps {
  value: string
  onChange: (v: string) => void
  onSend: () => void
  onStop: () => void
  onEnter: (e: React.KeyboardEvent<HTMLTextAreaElement>) => void
  sending: boolean
  disabled?: boolean
  uploading: boolean
  files: UploadedFile[]
  onRemoveFile: (index: number) => void
  onOpenFile: (file: UploadedFile) => void
  onUpload: (file: File) => void
 models: string[]
 model: string
 defaultModel: string
 onModelChange: (m: string) => void
  // 任务运行中排队（延时发送）的内容
  pending: PendingInput[]
  onRemovePending: (index: number) => void
}

const formatSize = (bytes?: number) => {
  if (bytes == null) return ''
  if (bytes < 1024) return `${bytes}B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(2)}KB`
  return `${(bytes / 1024 / 1024).toFixed(2)}MB`
}

const fileExt = (name: string) => {
  const i = name.lastIndexOf('.')
  return i >= 0 ? name.slice(i + 1).toLowerCase() : 'file'
}

// 模块按钮统一样式：矩形边框模块（对齐发送键的圆形模块风格，整体放大）
const moduleBtnStyle: React.CSSProperties = {
  height: 40,
  borderRadius: 10,
  border: '1px solid var(--border-color)',
  background: 'var(--bg-tertiary)',
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
}

/**
 * 聊天输入区（大输入框布局）。
 * 注意：Dropdown 的直接子元素必须是按钮本体，中间不能包 Tooltip，
 * 否则 antd 的点击触发会被 Tooltip 吞掉导致无法点开菜单。
 * 工具栏按区域组织，后续要在输入框加工具栏项，直接在左侧区域追加即可。
 */
export default function ChatComposer({
  value, onChange, onSend, onEnter, sending, uploading, disabled = false,
  onStop,
  files, onRemoveFile, onOpenFile, onUpload,
 models, model, onModelChange,
 defaultModel,
 pending, onRemovePending,
}: ChatComposerProps) {
  const { models: availableModels, modelLabel, loaded } = useModelDirectory()
  const [dragActive, setDragActive] = useState(false)
  const fileInputRef = useRef<HTMLInputElement>(null)
  // 输入框自适应：autoSize 3→8 行（96px→240px），放大按钮撑到对话区 90%
  const textAreaRef = useRef<any>(null)
  const [expanded, setExpanded] = useState(false)
  const [expandedHeight, setExpandedHeight] = useState(0)
  const handleExpand = () => {
    const chatEl = document.querySelector('.chat-messages') as HTMLElement | null
    setExpandedHeight(Math.floor((chatEl?.clientHeight || 400) * 0.9))
    setExpanded(true)
  }
  // 流式中无输入=停止态（已发送可停止）；有新输入=发送态（排队）
  const showStop = sending && !value.trim() && files.length === 0

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault()
    if (disabled) return
    setDragActive(false)
    const droppedFiles = Array.from(e.dataTransfer.files)
    if (!droppedFiles.length) {
      message.warning('请拖入本地图片或文件；网页图片请先保存到本地')
      return
    }
    droppedFiles.forEach(onUpload)
  }

  const handlePaste = (event: React.ClipboardEvent<HTMLTextAreaElement>) => {
    if (disabled) return
    const images = Array.from(event.clipboardData.items)
      .filter((item) => item.kind === 'file' && item.type.startsWith('image/'))
      .map((item) => item.getAsFile())
      .filter((file): file is File => file !== null)
    if (!images.length) return
    event.preventDefault()
    images.forEach((image, index) => {
      const extension = image.type.split('/')[1]?.replace('jpeg', 'jpg') || 'png'
      const filename = `screenshot-${Date.now()}-${index}.${extension}`
      onUpload(new File([image], filename, { type: image.type }))
    })
  }

  return (
    <div
      onDragOver={(e) => { e.preventDefault(); setDragActive(true) }}
      onDragLeave={() => setDragActive(false)}
      onDrop={handleDrop}
      style={{
        position: 'relative',
        border: dragActive ? '2px dashed var(--color-primary)' : '1px solid var(--border-color)',
        borderRadius: 26,
        background: 'var(--bg-primary)',
        boxShadow: '0 2px 12px rgba(0,0,0,0.06)',
        transition: 'border-color 0.15s ease',
      }}
    >
      {dragActive && (
        <div style={{
          position: 'absolute', inset: 0, borderRadius: 26, zIndex: 2, pointerEvents: 'none',
          background: 'var(--color-primary-bg)', display: 'flex', alignItems: 'center', justifyContent: 'center',
          color: 'var(--color-primary)', fontWeight: 500, fontSize: 13.5,
        }}>
          松开鼠标上传文件
        </div>
      )}

      {files.length > 0 && (
        <div style={{ display: 'flex', gap: 10, padding: '12px 14px 0', flexWrap: 'wrap' }}>
          {files.map((f, i) => (
            <div
              key={`${f.stored_name}-${i}`}
              onClick={() => onOpenFile(f)}
              style={{
                width: 170, border: '1px solid var(--border-color)', borderRadius: 10,
                padding: '8px 10px', cursor: 'pointer', background: 'var(--bg-secondary)',
              }}
            >
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <span style={{ fontSize: 12, fontWeight: 500, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {f.original_name}
                </span>
                <CloseOutlined
                  onClick={(e) => { e.stopPropagation(); onRemoveFile(i) }}
                  style={{ fontSize: 12, color: 'var(--text-tertiary)', flexShrink: 0, marginLeft: 6 }}
                />
              </div>
              <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 10.5, color: 'var(--text-tertiary)', marginTop: 6 }}>
                <span>{fileExt(f.original_name)}</span>
                <span>{formatSize(f.size)}</span>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* 待发送区：任务运行中点发送的内容挂在这里，等下一轮 tool_calls 注入给模型 */}
      {pending.length > 0 && (
        <div style={{ padding: '12px 14px 0' }}>
          {pending.map((p, i) => (
            <div
              key={`pending-${i}`}
              style={{
                border: '1px dashed var(--border-color)', borderRadius: 10,
                padding: '8px 10px', marginBottom: 8, background: 'var(--bg-secondary)',
              }}
            >
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <span style={{ fontSize: 11, fontWeight: 600, color: 'var(--color-primary)' }}>待发送</span>
                <CloseOutlined
                  onClick={() => onRemovePending(i)}
                  style={{ fontSize: 12, color: 'var(--text-tertiary)', cursor: 'pointer' }}
                />
              </div>
              {p.content && (
                <div style={{
                  fontSize: 12.5, color: 'var(--text-primary)', marginTop: 4, whiteSpace: 'pre-wrap',
                  display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical', overflow: 'hidden',
                }}>
                  {p.content}
                </div>
              )}
              {p.files.length > 0 && (
                <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginTop: 6 }}>
                  {p.files.map((f, fi) => (
                    <span key={`${f.stored_name}-${fi}`} style={{
                      fontSize: 11, color: 'var(--text-secondary)', border: '1px solid var(--border-color)',
                      borderRadius: 6, padding: '1px 6px', background: 'var(--bg-primary)',
                      maxWidth: 160, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', display: 'inline-block',
                    }}>
                      {f.original_name}
                    </span>
                  ))}
                </div>
              )}
            </div>
          ))}
          <div style={{ fontSize: 12, color: 'var(--text-tertiary)', paddingBottom: 4 }}>
            将在下一轮 tool_calls 上传给模型；按 Esc 停止此对话并立即上传给模型
          </div>
        </div>
      )}

      <Input.TextArea
        disabled={disabled}
        ref={textAreaRef}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={onEnter}
        onPaste={handlePaste}
        placeholder={sending
          ? '任务运行中，可继续输入；Enter/发送将排队，下一轮 tool_calls 上传给模型'
          : '输入消息...（Enter 发送，Shift+Enter 换行）'}
        bordered={false}
       autoSize={expanded ? false : { minRows: 3, maxRows: 8 }}
        style={{ padding: '14px 18px 6px', fontSize: 15, resize: 'none', overflowY: 'auto', minHeight: 96, ...(expanded ? { height: expandedHeight } : {}) }}
      />

      <Button
        type="text"
        size="small"
        title={expanded ? '收起输入框' : '放大输入框'}
        icon={expanded ? <CompressOutlined /> : <ExpandAltOutlined />}
        onClick={() => expanded ? setExpanded(false) : handleExpand()}
        style={{ position: 'absolute', top: 4, right: 8, zIndex: 3 }}
      />

      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '6px 14px 12px' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          {/* 点击 + 号直接打开系统文件选择，不再经过"上传文档"二级菜单 */}
          <Button
            title="添加附件"
            disabled={disabled}
            onClick={() => fileInputRef.current?.click()}
            style={{ ...moduleBtnStyle, width: 40, fontSize: 18 }}
            icon={<PlusOutlined />}
          />
          <input
            ref={fileInputRef}
            type="file"
            multiple
            style={{ display: 'none' }}
            onChange={(e) => {
              Array.from(e.target.files || []).forEach((f) => onUpload(f))
              e.target.value = ''
            }}
          />
          <Dropdown
            trigger={['click']}
            menu={{
              items: (loaded ? availableModels : models).map((modelId) => ({ key: modelId, label: modelLabel(modelId) })),
              onClick: ({ key }) => onModelChange(key),
              selectedKeys: model ? [model] : [],
            }}
            disabled={sending || disabled}
          >
            <Button
              title={sending ? '任务运行中不能切换模型（当前任务继续用启动时所选模型）' : '选择本次对话使用的模型'}
              disabled={sending || disabled}
              style={{ ...moduleBtnStyle, padding: '0 14px', fontSize: 16, gap: 6, color: 'var(--text-secondary)' }}
           >
              {modelLabel(model || defaultModel || '模型')} <span style={{ fontSize: 12 }}>▾</span>
           </Button>
          </Dropdown>
        </div>
        <Button
          title={showStop ? '停止当前任务' : sending ? '排队发送：下一轮 tool_calls 上传给模型' : '发送'}
          type="primary"
          danger={showStop}
          shape="circle"
          style={{ width: 40, height: 40, fontSize: 18 }}
          icon={showStop ? <StopOutlined /> : <ArrowUpOutlined />}
          onClick={showStop ? onStop : onSend}
          disabled={disabled || (!showStop && (uploading || (!value.trim() && files.length === 0)))}
        />
      </div>
    </div>
  )
}
