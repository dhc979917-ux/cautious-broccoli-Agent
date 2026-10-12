import { useEffect, useMemo, useRef, useState } from 'react'
import { Alert, Breadcrumb, Button, Empty, Segmented, Space, Spin } from 'antd'
import { FileOutlined, FolderOutlined, LockOutlined } from '@ant-design/icons'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import type { HubFile, FilePreview } from '../../api/skillHub'
import './index.css'

type Entry = { path: string; name: string; directory: boolean; bytes: number }
type Props = { files: HubFile[]; scope?: string; canPreview: boolean; loadFile: (path: string) => Promise<FilePreview> }

export default function SkillFileBrowser({ files, scope = '', canPreview, loadFile }: Props) {
  const base = scope === '.' ? '' : scope
  const [directory, setDirectory] = useState(base)
  const [selected, setSelected] = useState<FilePreview | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [mode, setMode] = useState<string | number>('预览')
  const sequence = useRef(0)
  useEffect(() => { ++sequence.current; setDirectory(base); setSelected(null); setError(''); setLoading(false); return () => { ++sequence.current } }, [base, files, canPreview])
  const entries = useMemo(() => {
    const prefix = directory ? directory + '/' : ''
    const rows = new Map<string, Entry>()
    for (const file of files) {
      if (!file.path.startsWith(prefix)) continue
      const relative = file.path.slice(prefix.length)
      const name = relative.split('/')[0]
      if (!name) continue
      const folder = relative.includes('/')
      if (!rows.has(name) || folder) rows.set(name, { name, path: prefix + name, directory: folder, bytes: file.bytes })
    }
    return [...rows.values()].sort((a, b) => Number(b.directory) - Number(a.directory) || a.name.localeCompare(b.name))
  }, [files, directory])
  const folder = (path: string) => { ++sequence.current; setDirectory(path); setSelected(null); setError(''); setLoading(false) }
  const open = async (entry: Entry) => {
    if (entry.directory) { folder(entry.path); return }
    if (!canPreview) return
    const request = ++sequence.current
    setSelected(null); setError(''); setLoading(true); setMode('预览')
    try { const result = await loadFile(entry.path); if (request === sequence.current) setSelected(result) }
    catch (e: any) { if (request === sequence.current) setError(e?.response?.data?.detail || e.message || '文件加载失败') }
    finally { if (request === sequence.current) setLoading(false) }
  }
  const relativeDirectory = directory.slice(base.length).replace(/^\//, '')
  const segments = relativeDirectory ? relativeDirectory.split('/') : []
  const crumbs = [{ title: <button onClick={() => folder(base)}>根目录</button> }, ...segments.map((part, index) => ({ title: <button onClick={() => folder([base, ...segments.slice(0, index + 1)].filter(Boolean).join('/'))}>{part}</button> }))]
  if (selected) crumbs.push({ title: <button onClick={() => setSelected(null)}>{selected.path.split('/').pop()}</button> })
  return <section className="skill-read-browser" aria-label="逐层文件浏览">
    {!canPreview && <Alert showIcon type="info" icon={<LockOutlined />} message="未开放派生许可：仅可查看目录结构，不能预览文件内容。" />}
    <Breadcrumb items={crumbs} />
    {error && <Alert type="error" showIcon message={error} closable onClose={() => setError('')} />}
    <Spin spinning={loading}>
      {selected ? <>
        <Space wrap className="skill-read-toolbar"><Button onClick={() => folder(directory)}>返回目录</Button><span>{selected.path.split('/').pop()}</span>{selected.content !== null && selected.path.toLowerCase().endsWith('.md') && <Segmented options={['预览', '源码']} value={mode} onChange={setMode} />}</Space>
        {selected.content === null ? <Empty description="此文件不支持文本预览。" /> : mode === '预览' && selected.path.toLowerCase().endsWith('.md') ? <article className="skill-read-markdown"><ReactMarkdown remarkPlugins={[remarkGfm]} components={{ a: ({ children }) => <span>{children}</span>, img: ({ alt }) => <span>{alt || '图片链接'}</span> }}>{selected.content}</ReactMarkdown></article> : <pre className="skill-read-source">{selected.content.split('\n').map((line, index) => <div key={index}><span aria-hidden="true">{index + 1}</span><code>{line || '\u00a0'}</code></div>)}</pre>}
      </> : <div className="skill-read-directory">
        <div className="skill-read-list-heading"><span>名称</span><span>大小 / 类型</span></div>
        {directory !== base && <button onClick={() => folder(directory.split('/').slice(0, -1).join('/'))}><FolderOutlined /><span>..</span><small>上级目录</small></button>}
        {entries.map(entry => <button key={entry.path} disabled={!entry.directory && !canPreview} onClick={() => void open(entry)} title={!entry.directory && !canPreview ? '管理员尚未开放派生许可' : entry.name}>{entry.directory ? <FolderOutlined /> : <FileOutlined />}<span>{entry.name}</span><small>{entry.directory ? '目录' : `${Math.ceil(entry.bytes / 1024)} KB`}</small></button>)}
        {!entries.length && <Empty description="此目录暂无文件" />}
      </div>}
    </Spin>
  </section>
}
