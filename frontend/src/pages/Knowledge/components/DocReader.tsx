import { useMemo, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { Button, Drawer, Empty, Input, Modal, Space, Spin, Tag, Tooltip, message } from 'antd'
import { EditOutlined, SaveOutlined, CloseOutlined, DeleteOutlined } from '@ant-design/icons'
import { UndoOutlined } from '@ant-design/icons'
import { updateDocContent, assistDocument } from '../../../api/knowledge'
import type { KnowledgeDocInfo, Backlink, DocAssist } from '../../../api/knowledge'
import { useAuthStore } from '../../../store/authStore'

interface Props {
  doc: (KnowledgeDocInfo & { content?: string | null }) | null
  backlinks: Backlink[]
  onWikilink?: (name: string) => void
  canEdit?: boolean
  canDelete?: boolean
  onSoftDelete?: () => void
  onHardDelete?: () => void
  onSaved?: (data: KnowledgeDocInfo & { content?: string | null }) => void
  onRestore?: () => void
}

function stripFrontmatter(md: string): string {
  let s = md.replace(/^\ufeff/, '')
  if (s.startsWith('---')) {
    const end = s.indexOf('\n---', 3)
    if (end !== -1) return s.slice(end + 4).trim()
  }
  return s
}

function preprocessWikilinks(md: string): string {
  return (md || '').replace(/!\[\[([^\]]+)\]\]/g, (_, inner) => {
    const [tgt] = inner.split('|')
    return `![](${tgt})`
  }).replace(/\[\[([^\]]+)\]\]/g, (_, inner) => {
    const [tgt, alias] = inner.split('|')
    const label = (alias || tgt).split('#')[0]
    return `[${label}](wikilink:${tgt.split('#')[0]})`
  })
}

export default function DocReader({ doc, backlinks, onWikilink, canEdit, canDelete, onSoftDelete, onHardDelete, onSaved, onRestore }: Props) {
  const [editing, setEditing] = useState(false)
  const [editContent, setEditContent] = useState('')
  const [saving, setSaving] = useState(false)
  const [pdfUrl, setPdfUrl] = useState<string | null>(null)
  const [pdfName, setPdfName] = useState('')
  const [assistOpen, setAssistOpen] = useState(false)
  const [assist, setAssist] = useState<DocAssist | null>(null)
  const [assistLoading, setAssistLoading] = useState(false)
  const content = useMemo(() => preprocessWikilinks(stripFrontmatter(doc?.content || '')), [doc?.content])

  const startEdit = () => {
    setEditContent(doc?.content || '')
    setEditing(true)
  }

  const openAssist = async () => {
    if (!doc) return
    setAssistOpen(true); setAssistLoading(true); setAssist(null)
    try {
      setAssist(await assistDocument(doc.id))
    } catch (e: any) {
      message.error(e.response?.data?.detail || 'AI 帮读失败')
      setAssistOpen(false)
    } finally {
      setAssistLoading(false)
    }
  }

  const saveEdit = async () => {
    if (!doc) return
    setSaving(true)
    try {
      const data = await updateDocContent(doc.id, editContent)
      message.success('Saved')
      setEditing(false)
      onSaved?.(data)
    } catch (e: any) {
      message.error(e.response?.data?.detail || e.message || 'Save failed')
    } finally {
      setSaving(false)
    }
  }

  if (!doc) return <Empty description="Select a note to view" style={{ marginTop: 60 }} />

  return (
    <div className={`docreader${doc.status === 'deleted' ? ' docreader-deleted' : ''}`}>
      <div className="docreader-head">
        <span className="docreader-title">{doc.title || doc.filename}</span>
        {doc.category && <Tag color="green">{doc.category}</Tag>}
        {(doc.tags || []).map((t) => <Tag key={t} color="blue">{t}</Tag>)}
        {doc.vault_path && <span className="docreader-path">{doc.vault_path}</span>}
        <Tooltip title="AI 帮读（摘要 / 要点 / 相关）">
          <Button type="text" size="small" loading={assistLoading} onClick={openAssist}>✨ 帮读</Button>
        </Tooltip>
        {canEdit && !editing && (
          <Tooltip title="Edit">
            <Button type="text" size="small" icon={<EditOutlined />} onClick={startEdit} />
          </Tooltip>
        )}
        {canDelete && !editing && (
          <>
            {doc.status === 'deleted' ? (
              <Tooltip title="恢复">
                <Button type="text" size="small" icon={<UndoOutlined />} onClick={() => onRestore?.()} />
              </Tooltip>
            ) : (
              <Tooltip title="软删除">
                <Button type="text" size="small" icon={<DeleteOutlined />} onClick={() => onSoftDelete?.()} />
              </Tooltip>
            )}
            <Tooltip title="完全删除">
              <Button type="text" size="small" danger icon={<DeleteOutlined />} onClick={() => {
                Modal.confirm({
                  title: '完全删除',
                  content: '将删除此笔记、对应源文件及向量数据，不可恢复。确定？',
                  okText: '确定删除', okButtonProps: { danger: true }, cancelText: '取消',
                  onOk: () => onHardDelete?.(),
                })
              }} />
            </Tooltip>
          </>
        )}
        {editing && (
          <Space size="small">
            <Button type="primary" size="small" icon={<SaveOutlined />} loading={saving} onClick={saveEdit}>Save</Button>
            <Button size="small" icon={<CloseOutlined />} onClick={() => setEditing(false)}>Cancel</Button>
          </Space>
        )}
      </div>
      <div className="docreader-body">
        {editing ? (
          <Input.TextArea
            value={editContent}
            onChange={(e) => setEditContent(e.target.value)}
            autoSize={{ minRows: 20, maxRows: 40 }}
            style={{ fontFamily: 'monospace', fontSize: 13 }}
          />
        ) : (
          <ReactMarkdown
            remarkPlugins={[remarkGfm]}
            components={{
              img: ({ src, alt }: any) => {
                const s = String(src || '')
                if (s && !/^(https?:|data:|wikilink:)/.test(s) && doc?.kb_id) {
                  const token = useAuthStore.getState().token
                  const url = `/api/v1/knowledge/vault-files/${doc.kb_id}?path=${encodeURIComponent(s)}&token=${token}`
                  return <img src={url} alt={alt || ''} style={{ maxWidth: '100%', borderRadius: 4 }} />
                }
                return <img src={s} alt={alt || ''} style={{ maxWidth: '100%' }} />
              },
              a: ({ node, href, children, ...rest }: any) => {
                if (href && href.startsWith('wikilink:')) {
                  const name = decodeURIComponent(href.slice('wikilink:'.length))
                  return (
                    <a href="#" onClick={(e) => { e.preventDefault(); onWikilink?.(name) }} style={{ color: 'var(--color-primary)' }}>
                      {children}
                    </a>
                )
                }
                if (href && href.startsWith('/api/v1/knowledge/files/')) {
                  return (
                    <a href="#" onClick={async (e) => {
                      e.preventDefault()
                      try {
                        const token = useAuthStore.getState().token
                        const resp = await fetch(href, { headers: { Authorization: `Bearer ${token}` } })
                        if (!resp.ok) throw new Error('Download failed')
                        const rawBlob = await resp.blob()
                        const fname = decodeURIComponent(href.split('/').pop() || 'file')
                        const ext = fname.split('.').pop()?.toLowerCase() || ''
                        const mimeMap: Record<string, string> = {
                          pdf: 'application/pdf',
                          docx: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                          doc: 'application/msword',
                          xlsx: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                          pptx: 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
                        }
                        const mime = mimeMap[ext] || rawBlob.type || 'application/octet-stream'
                        const blob = rawBlob.type === mime ? rawBlob : new Blob([rawBlob], { type: mime })
                        if (pdfUrl) URL.revokeObjectURL(pdfUrl)
                        const url = URL.createObjectURL(blob)
                        setPdfUrl(url)
                        setPdfName(fname)
                      } catch { message.error('文件下载失败') }
                    }} style={{ color: 'var(--color-primary)' }}>
                      {children}
                    </a>
                  )
                }
                return <a href={href} target="_blank" rel="noreferrer" {...rest}>{children}</a>
              },
            }}
          >
            {content}
          </ReactMarkdown>
        )}
      </div>
      <div className="docreader-backlinks">
        <div className="docreader-subtitle">Backlinks ({backlinks.length})</div>
        {backlinks.length === 0 ? (
          <Empty description="No backlinks" image={Empty.PRESENTED_IMAGE_SIMPLE} />
        ) : (
          <div style={{ maxHeight: 110, overflowY: 'auto' }}>
            {backlinks.map((b, i) => (
              <div key={i} className="backlink-item">
                <Tag color="purple">{b.kind}</Tag>
                <span className="backlink-src">{b.src_title || `Doc#${b.src_id}`}</span>
                {b.src_vault_path && <div className="backlink-snip">{b.src_vault_path}</div>}
              </div>
            ))}
          </div>
        )}
      </div>

      <Drawer
        title={pdfName || '原文预览'}
        placement="right"
        width={900}
        open={!!pdfUrl}
        onClose={() => { if (pdfUrl) URL.revokeObjectURL(pdfUrl); setPdfUrl(null) }}
        destroyOnClose
      >
        {pdfUrl && (
          <iframe src={pdfUrl} style={{ width: '100%', height: '100%', border: 'none' }} title={pdfName} />
        )}
      </Drawer>

      <Drawer
        title={<>✨ AI 帮读{assist?.mode === 'local' && <Tag color="orange" style={{ marginLeft: 8 }}>本地模式</Tag>}</>}
        placement="right"
        width={480}
        open={assistOpen}
        onClose={() => setAssistOpen(false)}
      >
        {assistLoading ? (
          <div style={{ textAlign: 'center', marginTop: 60 }}><Spin /></div>
        ) : assist ? (
          <div>
            <div className="docreader-subtitle">摘要</div>
            <p style={{ fontSize: 13, lineHeight: 1.8 }}>{assist.summary || '（无）'}</p>
            {assist.points.length > 0 && (
              <>
                <div className="docreader-subtitle" style={{ marginTop: 16 }}>关键要点</div>
                <ul style={{ paddingLeft: 18, fontSize: 13 }}>
                  {assist.points.map((p, i) => <li key={i} style={{ marginBottom: 6 }}>{p}</li>)}
                </ul>
              </>
            )}
            {assist.questions.length > 0 && (
              <>
                <div className="docreader-subtitle" style={{ marginTop: 16 }}>你可能想问</div>
                {assist.questions.map((q, i) => (
                  <div key={i} style={{ fontSize: 13, color: 'var(--color-primary)', marginBottom: 6 }}>· {q}</div>
                ))}
              </>
            )}
            {(assist.related?.length ?? 0) > 0 && (
              <>
                <div className="docreader-subtitle" style={{ marginTop: 16 }}>相关文档（图谱反链）</div>
                {assist.related!.map((r, i) => (
                  <div key={i} style={{ fontSize: 13, marginBottom: 4 }}>📄 {r.title || `Doc#${r.doc_id}`}</div>
                ))}
              </>
            )}
          </div>
        ) : null}
      </Drawer>
    </div>
  )
}
