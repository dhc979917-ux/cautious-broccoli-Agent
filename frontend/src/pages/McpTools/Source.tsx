import { useEffect, useRef, useState } from 'react'
import { Alert, Button, Empty, Spin, message } from 'antd'
import { CopyOutlined, CodeOutlined } from '@ant-design/icons'
import { useParams } from 'react-router-dom'
import { getMcpSource, type McpSource } from '../../api/mcp'
import { readableError } from '../../api/governance'
import './index.css'

export function McpSourcePreview({ serverId, toolName }: { serverId: number; toolName: string }) {
  const [source, setSource] = useState<McpSource | null>(null)
  const [error, setError] = useState('')
  const target = useRef<HTMLDivElement>(null)
  const codeBox = useRef<HTMLDivElement>(null)
  useEffect(() => {
    let active = true
    setSource(null); setError('')
    getMcpSource(serverId, toolName).then((result) => { if (active) setSource(result) })
      .catch((cause) => { if (active) setError(readableError(cause)) })
    return () => { active = false }
  }, [serverId, toolName])
  useEffect(() => {
    const frame = requestAnimationFrame(() => {
      const container = codeBox.current
      const line = target.current
      if (container && line) container.scrollTop = line.offsetTop - container.offsetTop - container.clientHeight / 2 + line.clientHeight / 2
    })
    return () => cancelAnimationFrame(frame)
  }, [source])
  async function copy() {
    try { await navigator.clipboard.writeText(source?.content || ''); message.success('源码已复制') }
    catch { message.error('浏览器不允许复制，请手动选择源码') }
  }
  return <div className="mcp-source-page"><header className="mcp-source-header"><div><h2><CodeOutlined /> {toolName}</h2>
    <p>{source?.filename || 'Python 源码'}{source?.origin ? ` · ${source.origin}` : ''} · 只读预览</p></div>
    {source?.available && <Button icon={<CopyOutlined />} onClick={() => void copy()}>复制源码</Button>}</header>
    {error ? <Alert type="error" showIcon message={error} /> : !source ? <Spin /> : !source.available
      ? <Empty description={source.reason} /> : <div ref={codeBox} className="mcp-source-code" role="region" aria-label="Python 源码">
        {(source.content || '').split('\n').map((line, index) => <div key={index} ref={index + 1 === source.line ? target : undefined}
          className={index + 1 === source.line ? 'is-target' : ''}><span className="mcp-source-line">{index + 1}</span><code>{line || ' '}</code></div>)}
      </div>}</div>
}

export default function McpSourcePage() {
  const { serverId, toolName } = useParams()
  return <McpSourcePreview serverId={Number(serverId)} toolName={toolName || ''} />
}
