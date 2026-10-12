import SkillEvolutionPanel from '../../../components/SkillEvolutionPanel'
import { useNavigate } from 'react-router-dom'
import { useAuthStore } from '../../../store/authStore'
import { useEffect, useState } from 'react'
import { Alert, Button, Drawer, Empty, Input, List, message, Modal, Popconfirm, Select, Space, Table, Tabs, Tag, Typography } from 'antd'
import { DownloadOutlined, FileOutlined, PlusOutlined, UploadOutlined } from '@ant-design/icons'
import * as api from '../../../api/skillHub'
import DetailsForm, { blankDetails } from '../DetailsForm'
const fail = (e: any) => message.error(e?.response?.data?.detail || e.message || '操作失败，请重试')

export default function ResourceEditor({ id, onClose, onChange, onShare, onSubmit }: { id: string | null; onClose: () => void; onChange: () => void; onShare: (r: api.HubResource) => void; onSubmit: (r: api.HubResource) => void }) {
  const navigate = useNavigate()
  const [row, setRow] = useState<api.HubResource | null>(null)
  const [busy, setBusy] = useState(false)
  const [path, setPath] = useState('SKILL.md')
  const [text, setText] = useState('')
  const [fileKind, setFileKind] = useState('text')
  const [version, setVersion] = useState('')
  const [scripts, setScripts] = useState<Record<string, string>>({})
  const [script, setScript] = useState('')
  const [sample, setSample] = useState('')
  const [changes, setChanges] = useState<api.FileChange[]>([])
  const [uploadFile, setUploadFile] = useState<File | null>(null)
  const [rename, setRename] = useState('')
  const [dependenciesText, setDependenciesText] = useState('{}')
  const [versionFiles, setVersionFiles] = useState<api.HubFile[]>([])
  const load = async () => { if (!id) return; const next = await api.hubDetail(id); setRow(next); setVersion(next.selected_version_id || ''); setVersionFiles(next.files || []); setDependenciesText(JSON.stringify(next.metadata?.dependencies || {}, null, 2)); setChanges([]) }
  useEffect(() => { if (id) { setRow(null); setPath('SKILL.md'); setScripts({}); setVersionFiles([]); void load().catch(fail) } }, [id])
  useEffect(() => { if (id && version && versionFiles.some(f => f.path === path)) api.hubFile(id, version, path).then(p => { setText(p.content || ''); setFileKind(p.kind) }).catch(fail) }, [id, version, path, row?.revision])
  const act = async (task: () => Promise<unknown>) => { setBusy(true); try { await task(); await load(); onChange() } catch (e) { fail(e) } finally { setBusy(false) } }
  const editable = !!row && row.owner_id !== null && !row.archived && !row.blocked && version === (row.draft_version_id || row.published_version_id)
  const download = async () => { if (!id) return; try { const blob = await api.hubDownload(id, version, path); const url = URL.createObjectURL(blob); const a = document.createElement('a'); a.href = url; a.download = path.split('/').pop() || 'file'; a.click(); URL.revokeObjectURL(url) } catch (e) { fail(e) } }
  const checkUpload = async (file: File) => { if (!row) return; setBusy(true); try { const plan = await api.hubUpload(row.id, file, row.revision, false); setChanges(plan.changes); setUploadFile(file) } catch (e) { fail(e) } finally { setBusy(false) } }
  return <Drawer open={!!id} onClose={onClose} width="min(1100px, 96vw)" title={row?.title || '技能内容'} destroyOnClose>
    {!row ? <Empty description="正在加载技能" /> : <>
      <Space wrap className="hub-editor-toolbar"><Tag>{row.kind === 'package' ? '整包' : '独立技能'}</Tag><Tag>{row.source_title ? `来源：${row.source_title}` : '个人原创 / 导入'}</Tag>
        <Button onClick={() => onShare(row)} disabled={!row.published_version_id || row.blocked}>分享</Button><Button onClick={() => onSubmit(row)} disabled={!row.published_version_id || row.blocked}>申请社区上架 / 更新</Button>
      </Space>
      {!!row.provenance?.length && <Typography.Paragraph>来源链：{row.provenance.map(p => p.title).reverse().join(' → ')} → {row.title}（固定来源版本）</Typography.Paragraph>}
      <Popconfirm title="进入真实对话试用已发布的自用版本？" description="会使用你的实际模型和工具权限，可能产生费用或外部操作；草稿不参与。" onConfirm={async () => { try { const trial = await api.hubTrial(row.id); useAuthStore.getState().setSelectedSkills(trial.skills); onClose(); navigate('/chat?panel=skill') } catch (e) { fail(e) } }}><Button disabled={!row.published_version_id || row.archived || row.blocked}>真实业务试用</Button></Popconfirm>
      <Alert type="info" showIcon message="保存只生成草稿，不改变正在使用的版本。个人发布、分享版本和社区上架分别管理。" />
      <SkillEvolutionPanel resourceId={row.id} onPublished={() => { void load(); onChange() }} />
      <Tabs items={[
        { key: 'files', label: '文件与草稿', children: <>
          <Space wrap className="hub-editor-toolbar"><Select aria-label="查看版本" value={version} style={{ minWidth: 260 }} onChange={v => { setVersion(v); api.hubVersionFiles(row.id, v).then(files => { setVersionFiles(files); if (!files.some(f => f.path === path)) setPath(files[0]?.path || 'SKILL.md') }).catch(fail) }} options={(row.versions || []).map(v => ({ value: v.id, label: `v${v.number} · ${v.state === 'draft' ? '草稿' : '已发布自用'} · ${v.test_status}` }))} />
            <Button icon={<DownloadOutlined />} onClick={download}>下载当前文件</Button>
            <label className="hub-upload-button"><UploadOutlined /> ZIP更新<input type="file" accept=".zip" disabled={busy || !editable} onChange={e => { if (e.target.files?.[0]) void checkUpload(e.target.files[0]); e.target.value = '' }} /></label>
          </Space>
          <div className="hub-editor-grid"><nav aria-label="技能文件列表" className="hub-file-list">{versionFiles.map(f => <button key={f.path} className={f.path === path ? 'active' : ''} onClick={() => setPath(f.path)}><FileOutlined /><span>{f.path}</span></button>)}</nav>
            <div><Input aria-label="文件路径" value={path} onChange={e => setPath(e.target.value)} /><Input.TextArea aria-label="文件内容" value={text} onChange={e => setText(e.target.value)} rows={18} readOnly={!editable || fileKind !== 'text'} className="hub-code" />
              {fileKind !== 'text' && <Alert message="该文件为图片或二进制，使用下载查看。" />}
              <Space wrap className="hub-editor-toolbar"><Button type="primary" loading={busy} disabled={!editable} onClick={() => void act(() => api.hubEdit(row.id, { revision: row.revision, path, content: text }))}>保存草稿</Button>
                <Button icon={<PlusOutlined />} disabled={!editable} onClick={() => { setPath('references/new.md'); setText(''); setFileKind('text') }}>新增文件</Button>
                <Popconfirm title="删除此文件并生成新草稿？" onConfirm={() => act(() => api.hubEdit(row.id, { revision: row.revision, path, operation: 'delete' }))}><Button danger disabled={!editable}>删除文件</Button></Popconfirm>
              </Space>
              <Space.Compact block><Input placeholder="新的文件路径" value={rename} onChange={e => setRename(e.target.value)} /><Button disabled={!editable || !rename} onClick={() => void act(() => api.hubEdit(row.id, { revision: row.revision, path, operation: 'rename', new_path: rename }))}>重命名</Button></Space.Compact>
            </div>
          </div>
        </> },
        { key: 'versions', label: '检查、测试与版本', children: <>
          <Alert type="warning" showIcon message="隔离测试不调用真实模型或工具；通过不代表业务结果正确，也不是完整安全认证。" />
          <Space wrap className="hub-editor-toolbar"><Select allowClear placeholder="可选 Python 测试脚本" value={script || undefined} onChange={v => setScript(v || '')} options={versionFiles.filter(f => f.path.endsWith('.py')).map(f => ({ label: f.path, value: f.path }))} style={{ minWidth: 280 }} /><Button loading={busy} disabled={!row.draft_version_id || row.blocked} onClick={() => void act(async () => { const result = await api.hubTest(row.id, script, sample, scripts); if (result.test_status !== 'passed') message.error('测试未通过，请查看报告'); else message.success('检查与隔离测试通过') })}>检查与隔离测试</Button><Button type="primary" loading={busy} disabled={!row.draft_version_id || row.blocked} onClick={() => void act(() => api.hubPublish(row.id, row.revision))}>发布自用版本</Button></Space>
          {row.kind === 'package' && <><Typography.Paragraph>整包可按成员指定测试脚本。声明了依赖的成员需要模拟依赖测试，纯烟测不能代替。</Typography.Paragraph>{versionFiles.filter(f => f.path === 'SKILL.md' || f.path.endsWith('/SKILL.md')).map(f => { const location = f.path === 'SKILL.md' ? '.' : f.path.slice(0, -9); const prefix = location === '.' ? '' : location + '/'; return <Space key={location} className="hub-editor-toolbar"><span>{location}</span><Select allowClear placeholder="此成员测试脚本" style={{ minWidth: 220 }} value={scripts[location]} onChange={value => setScripts(s => { const next = { ...s }; if (value) next[location] = value; else delete next[location]; return next })} options={versionFiles.filter(x => x.path.startsWith(prefix) && x.path.endsWith('.py')).map(x => ({ value: x.path.slice(prefix.length), label: x.path }))} /></Space> })}</>}
          <Input.TextArea rows={3} placeholder="测试输入（仅模拟依赖）" value={sample} onChange={e => setSample(e.target.value)} />
          <Table rowKey="id" pagination={false} dataSource={row.versions || []} columns={[
            { title: '版本', render: (_, v) => `v${v.number}` }, { title: '状态', dataIndex: 'state' }, { title: '检查', dataIndex: 'test_status' },
            { title: '操作', render: (_, v) => <Space><Button size="small" onClick={() => { setVersion(v.id); api.hubVersionFiles(row.id, v.id).then(setVersionFiles).catch(fail); message.info('已选择版本，请到文件页查看') }}>查看</Button>{row.published_version_id && <Button size="small" onClick={() => api.hubDiff(row.id, row.published_version_id!, v.id).then(setChanges).catch(fail)}>对比当前版本</Button>}{v.state === 'published' && v.id !== row.published_version_id && <Popconfirm title="回滚自用版本？社区和分享版本不会变化。" onConfirm={() => act(() => api.hubPublish(row.id, row.revision, v.id))}><Button size="small">回滚</Button></Popconfirm>}</Space> },
          ]} />
          {(row.versions || []).filter(v => Object.keys(v.test_report || {}).length > 0).slice(0, 3).map(v => <div key={v.id}><Typography.Title level={5}>v{v.number} 检查报告</Typography.Title><pre className="hub-report">{JSON.stringify(v.test_report, null, 2)}</pre></div>)}
        </> },
        { key: 'details', label: '介绍与依赖', children: <><DetailsForm busy={busy} value={row.metadata || blankDetails(row.title, row.summary)} onSave={d => void act(() => api.hubMetadata(row.id, row.revision, { ...d, dependencies: row.metadata?.dependencies || {} }))} /><Typography.Title level={5}>依赖声明</Typography.Title><Input.TextArea rows={6} value={dependenciesText} onChange={e => setDependenciesText(e.target.value)} /><Button onClick={() => { try { const dependencies = JSON.parse(dependenciesText); void act(() => api.hubMetadata(row.id, row.revision, { ...(row.metadata || blankDetails(row.title, row.summary)), dependencies })) } catch { message.error('依赖声明必须为有效JSON') } }}>保存依赖为新草稿</Button></> },
      ]} />
      <Modal title={uploadFile ? '确认ZIP更新（只保存草稿）' : '文件差异'} open={changes.length > 0 || !!uploadFile} onCancel={() => { setChanges([]); setUploadFile(null) }} footer={uploadFile ? <Button type="primary" loading={busy} onClick={() => void act(async () => { await api.hubUpload(row.id, uploadFile, row.revision, true); setUploadFile(null); setChanges([]) })}>确认生成草稿</Button> : null}>
        <List dataSource={changes} renderItem={c => <List.Item><Tag>{c.action}</Tag>{c.path}</List.Item>} locale={{ emptyText: '文件内容没有变化' }} />
      </Modal>
    </>}
  </Drawer>
}
