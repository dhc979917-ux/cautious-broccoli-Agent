import { useEffect, useRef, useState } from 'react'
import { useParams } from 'react-router-dom'
import { Alert, Breadcrumb, Button, Empty, Input, Modal, Select, Space, Spin, Tag, message } from 'antd'
import { CopyOutlined, DownloadOutlined, EditOutlined, FileOutlined, FolderOutlined, ReloadOutlined } from '@ant-design/icons'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { useAuthStore } from '../../store/authStore'
import SkillResourceUpdate from '../../components/SkillResourceUpdate'
import useUnsavedFile from '../../hooks/useUnsavedFile'
import { readableError } from '../../api/governance'
import { resourceContent, resourceDownload, resourceEdit, resourceEntries, resourceHistory, resourceJob, type FileContent, type FileEntry, type ResourceHistory, type ResourceKind } from '../../api/skillResources'
import './index.css'

function formatTime(value: string) {
  const utc = /Z$|[+-]\d\d:\d\d$/.test(value) ? value : value + 'Z'
  return new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }).format(new Date(utc))
}
function size(bytes: number | null) { return bytes === null ? '文件夹' : bytes < 1024 ? `${bytes} B` : bytes < 1024 * 1024 ? `${(bytes / 1024).toFixed(1)} KiB` : `${(bytes / 1024 / 1024).toFixed(1)} MiB` }

export default function SkillVersions() {
  const { skillId, packageId } = useParams()
  const kind: ResourceKind = packageId ? 'package' : 'skill'
  const id = Number(packageId || skillId)
  const admin = useAuthStore((state) => state.user?.role === 'admin')
  const [history, setHistory] = useState<ResourceHistory | null>(null)
  const [selected, setSelected] = useState('')
  const [directory, setDirectory] = useState('')
  const [entries, setEntries] = useState<FileEntry[]>([])
  const [file, setFile] = useState<FileContent | null>(null)
  const [draft, setDraft] = useState('')
  const [editing, setEditing] = useState(false)
  const [markdown, setMarkdown] = useState(true)
  const [image, setImage] = useState('')
  const [busy, setBusy] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const sequence = useRef(0)
  const dirty = editing && draft !== file?.content
  useUnsavedFile(dirty)
  async function load(focus?: string) {
    const current = sequence.current
    setBusy(true); setError('')
    try { const next = await resourceHistory(kind, id); if (current === sequence.current) { setHistory(next); setSelected(focus || next.version_id) } }
    catch (cause) { if (current === sequence.current) setError(readableError(cause)) }
    finally { if (current === sequence.current) setBusy(false) }
  }
  useEffect(() => { sequence.current++; if (admin && Number.isInteger(id) && id > 0) { setSelected(''); setHistory(null); setFile(null); setDirectory(''); setEditing(false); setSaving(false); void load() }; return () => { sequence.current++ } }, [kind, id, admin])
  useEffect(() => {
    if (!selected) return
    let alive = true
    setBusy(true)
    resourceEntries(kind, id, selected, directory).then((next) => { if (alive) setEntries(next) }).catch((cause) => { if (alive) setError(readableError(cause)) }).finally(() => { if (alive) setBusy(false) })
    return () => { alive = false }
  }, [kind, id, selected, directory])
  useEffect(() => {
    if (!file || file.kind !== 'image') { setImage(''); return }
    let alive = true; let url = ''
    resourceDownload(kind, id, selected, file.path, true).then((blob) => { if (alive) { url = URL.createObjectURL(blob); setImage(url) } }).catch((cause) => { if (alive) setError(readableError(cause)) })
    return () => { alive = false; if (url) URL.revokeObjectURL(url) }
  }, [file?.path, file?.kind, selected, kind, id])
  function discard(action: () => void) { if (!dirty) { action(); return }; Modal.confirm({ title: '放弃未保存的文件修改？', okText: '放弃修改', cancelText: '继续编辑', onOk: action }) }
  function folder(path: string) { discard(() => { sequence.current++; setFile(null); setEditing(false); setDirectory(path) }) }
  function version(value: string) { discard(() => { sequence.current++; setFile(null); setEditing(false); setDirectory(''); setSelected(value) }) }
  async function open(entry: FileEntry) {
    if (entry.kind === 'directory') { folder(entry.path); return }
    discard(() => {
      const token = ++sequence.current; setBusy(true); setEditing(false)
      resourceContent(kind, id, selected, entry.path).then((next) => { if (token === sequence.current) { setFile(next); setDraft(next.content || ''); setMarkdown(true) } }).catch((cause) => { if (token === sequence.current) setError(readableError(cause)) }).finally(() => { if (token === sequence.current) setBusy(false) })
    })
  }
  async function download() {
    if (!file) return
    try { const blob = await resourceDownload(kind, id, selected, file.path); const url = URL.createObjectURL(blob); const link = document.createElement('a'); link.href = url; link.download = file.path.split('/').pop()!; link.click(); window.setTimeout(() => URL.revokeObjectURL(url), 1000) }
    catch (cause) { setError(readableError(cause)) }
  }
  async function copy() {
    const text = file?.content; if (text === null || text === undefined) return
    try { if (navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(text); else { const input = document.createElement('textarea'); input.value = text; document.body.appendChild(input); input.select(); const copied = document.execCommand('copy'); input.remove(); if (!copied) throw Error('复制失败，请手动选择源码'); }; message.success('文件内容已复制') }
    catch { message.error('复制失败，请手动选择源码') }
  }
  async function save() {
    if (!file || !history) return
    const current = sequence.current
    setSaving(true); setError('')
    try {
      let job = await resourceEdit(kind, id, { path: file.path, content: draft, version_id: selected, revision: history.revision, sha256: file.sha256 })
      while (job.id && !['done', 'failed', 'cancelled'].includes(job.status)) { await new Promise((resolve) => window.setTimeout(resolve, 1000)); if (current !== sequence.current) return; job = await resourceJob(kind, id, job.id) }
      if (current !== sequence.current) return
      if (job.status !== 'done') throw Error(job.message)
      const path = file.path; setEditing(false); message.success(job.message)
      const next = await resourceHistory(kind, id); const updated = await resourceContent(kind, id, next.version_id, path); if (current === sequence.current) { setHistory(next); setSelected(next.version_id); setFile(updated); setDraft(updated.content || '') }
    } catch (cause) { if (current === sequence.current) setError(cause instanceof Error && !('response' in cause) ? cause.message : readableError(cause)) }
    finally { if (current === sequence.current) setSaving(false) }
  }
  if (!admin) return <Alert type="error" showIcon message="无权查看 Skill 版本" />
  if (!Number.isInteger(id) || id <= 0) return <Empty description="无效的技能资源" />
  const crumbs = [{ title: <button onClick={() => folder('')}>根目录</button> }, ...directory.split('/').filter(Boolean).map((part, index, parts) => ({ title: <button onClick={() => folder(parts.slice(0, index + 1).join('/'))}>{part}</button> }))]
  return <section className="skill-files-page">
    <header className="skill-files-heading"><div><h1>{history?.name || '版本与文件'}</h1><span>{kind === 'package' ? '技能包' : 'Skill'}版本与文件</span></div><Space><SkillResourceUpdate key={`${kind}-${id}`} kind={kind} id={id} disabled={dirty || saving} onDone={() => { if (!saving) { setFile(null); setDirectory(''); void load() } }} /><Button icon={<ReloadOutlined />} disabled={saving} onClick={() => discard(() => { setFile(null); setEditing(false); void load() })}>刷新</Button></Space></header>
    {error && <Alert type="error" showIcon closable message={error} onClose={() => setError('')} />}
    <div className="skill-files-layout">
      <aside className="skill-version-list"><h2>版本记录</h2><Select className="skill-version-mobile" value={selected || undefined} onChange={version} options={history?.versions.map((item) => ({ value: item.id, label: `${item.label} · ${formatTime(item.created_at)}` }))} />
        <div className="skill-version-items">{history?.versions.map((item) => <button key={item.id} className={selected === item.id ? 'is-selected' : ''} onClick={() => version(item.id)}><strong>{item.label}{item.current && <Tag color="green">当前生效</Tag>}</strong><time>{formatTime(item.created_at)}</time></button>)}</div>
      </aside>
      <main className="skill-file-browser" aria-label="文件浏览器"><div className="skill-file-path"><Breadcrumb items={crumbs} />{file && <span>{file.path.split('/').pop()}</span>}</div>
        {busy && !history ? <Spin /> : file ? <>
          <div className="skill-file-toolbar"><span>{file.path} · {size(file.bytes)}</span><Space>{editing ? <><Button onClick={() => discard(() => { setEditing(false); setDraft(file.content || '') })} disabled={saving}>取消</Button><Button type="primary" loading={saving} onClick={() => void save()}>保存并生效</Button></> : <>{file.kind === 'text' && file.path.toLowerCase().endsWith('.md') && <Button onClick={() => setMarkdown(!markdown)}>{markdown ? '源码' : '预览'}</Button>}<Button icon={<EditOutlined />} disabled={!file.editable || selected !== history?.version_id || !history?.editable} onClick={() => setEditing(true)}>编辑</Button><Button icon={<DownloadOutlined />} onClick={() => void download()}>下载</Button><Button icon={<CopyOutlined />} disabled={file.kind !== 'text'} onClick={() => void copy()}>复制</Button></>}</Space></div>
          {selected !== history?.version_id && <div className="skill-file-readonly">历史版本只读；切换当前生效版本后可编辑。</div>}
          <div className="skill-file-content">{editing ? <Input.TextArea aria-label="文件编辑器" value={draft} onChange={(event) => setDraft(event.target.value)} spellCheck={false} disabled={saving} /> : file.kind === 'image' ? <img src={image} alt={file.path} /> : file.kind !== 'text' ? <Empty description="二进制或超过2MiB的文件不提供文本预览，请下载查看。" /> : markdown && file.path.toLowerCase().endsWith('.md') ? <article className="skill-markdown"><ReactMarkdown remarkPlugins={[remarkGfm]} components={{ a: ({ children }) => <span>{children}</span>, img: ({ alt }) => <span>{alt || '图片链接'}</span> }}>{file.content}</ReactMarkdown></article> : <pre className="skill-source">{file.content?.split('\n').map((line, index) => <div key={index}><span aria-hidden="true">{index + 1}</span><code>{line || '\u00a0'}</code></div>)}</pre>}</div>
        </> : <div className="skill-directory-list"><div className="skill-directory-header"><span>名称</span><span>大小／类型</span></div>{directory && <button onClick={() => folder(directory.split('/').slice(0, -1).join('/'))}><FolderOutlined /><span>..</span><small>上级目录</small></button>}{entries.map((entry) => <button key={entry.path} onClick={() => void open(entry)}>{entry.kind === 'directory' ? <FolderOutlined /> : <FileOutlined />}<span>{entry.name}</span><small>{size(entry.bytes)}</small></button>)}{!entries.length && !busy && <Empty description="此版本暂无文件" />}{busy && <Spin />}</div>}
      </main>
    </div>
  </section>
}
