import { useCallback, useEffect, useRef, useState } from 'react'
import { Alert, Button, Checkbox, Drawer, Empty, Form, Input, message, Modal, Popconfirm, Rate, Select, Space, Spin, Table, Tabs, Tag, Typography } from 'antd'
import { AppstoreOutlined, CopyOutlined, PlusOutlined, ReloadOutlined, SearchOutlined, ToolOutlined, UploadOutlined, UserOutlined } from '@ant-design/icons'
import * as api from '../../api/skillHub'
import SkillEvolutionPanel from '../../components/SkillEvolutionPanel'
import SkillFileBrowser from '../../components/SkillFileBrowser'
import PackageCards from './components/PackageCards'
import ResourceEditor from './components/ResourceEditor'
import CommunityDetail from './components/CommunityDetail'
import DetailsForm, { blankDetails, categories } from './DetailsForm'
import './index.css'
import './hub.css'
const originNames: Record<string, string> = { system: '系统', personal: '原创', imported: '导入', forked: '派生', installed: '社区安装', auto_forked: '自动派生', auto_created: '自动创建' }
const statusNames: Record<string, string> = { pending_review: '审核中', approved: '审核通过', rejected: '已驳回', withdrawn: '已撤回', published: '已上架', offline: '已下架', draft: '未上架' }
const fail = (e: any) => message.error(e?.response?.data?.detail || e.message || '操作失败，请重试')

export default function SkillSquare() {
  const [module, setModule] = useState('center')
  const [center, setCenter] = useState('system')
  const [mineTab, setMineTab] = useState('all')
  const [system, setSystem] = useState<api.HubResource[]>([])
  const [mine, setMine] = useState<api.HubResource[]>([])
  const [listings, setListings] = useState<api.HubListing[]>([])
  const [incoming, setIncoming] = useState<api.HubShare[]>([])
  const [outgoing, setOutgoing] = useState<api.HubShare[]>([])
  const [publications, setPublications] = useState<api.HubRequest[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [keyword, setKeyword] = useState('')
  const [category, setCategory] = useState('')
  const [sort, setSort] = useState('latest')
  const [tag, setTag] = useState('')
  const [origin, setOrigin] = useState('')
  const [editorId, setEditorId] = useState<string | null>(null)
  const [communityScope, setCommunityScope] = useState('')
  const [communityId, setCommunityId] = useState<string | null>(null)
  const [readRow, setReadRow] = useState<api.HubResource | null>(null)
  const [readScope, setReadScope] = useState('')
  const [packageView, setPackageView] = useState<{ resource?: api.HubResource; listing?: api.HubListing } | null>(null)
  const [packageBusy, setPackageBusy] = useState(false)
  const navigationSequence = useRef(0)
  const detailSequence = useRef(0)
  useEffect(() => () => { ++detailSequence.current; ++navigationSequence.current }, [])
  const [createOpen, setCreateOpen] = useState(false)
  const [importOpen, setImportOpen] = useState(false)
  const [folderFiles, setFolderFiles] = useState<File[]>([])
  const [importFile, setImportFile] = useState<File | null>(null)
  const [shareRow, setShareRow] = useState<api.HubResource | null>(null)
  const [submitRow, setSubmitRow] = useState<api.HubResource | null>(null)
  const [users, setUsers] = useState<{ id: number; name: string }[]>([])
  const [recipient, setRecipient] = useState<number>()
  const [allowFork, setAllowFork] = useState(false)
  const [expires, setExpires] = useState('')
  const [createForm] = Form.useForm()
  const [importForm] = Form.useForm()
  const load = useCallback(async () => {
    setLoading(true); setError('')
    try { const [a, b, c, d, e, f] = await Promise.all([api.hubSystem(), api.hubMine(), api.hubListings({ keyword, category, sort, tag }), api.hubShares(true), api.hubShares(), api.hubPublications()]); setSystem(a); setMine(b); setListings(c); setIncoming(d); setOutgoing(e); setPublications(f) } catch (e: any) { setError(e?.response?.data?.detail || '技能加载失败，请重试') } finally { setLoading(false) }
  }, [keyword, category, sort, tag])
  useEffect(() => { const timer = setTimeout(() => void load(), 180); return () => clearTimeout(timer) }, [load])
  const act = async (task: () => Promise<unknown>) => { setBusy(true); try { await task(); await load() } catch (e) { fail(e) } finally { setBusy(false) } }
  const openMine = (row: api.HubResource) => { setModule('mine'); setMineTab('all'); setCommunityId(null); setEditorId(row.id) }
  const openRead = async (id: string, scope = '') => { const sequence = ++detailSequence.current; try { const row = await api.hubDetail(id); if (sequence === detailSequence.current) { setReadScope(scope); setReadRow(row) } } catch (e) { if (sequence === detailSequence.current) fail(e) } }
  const openPackage = async (id: string, community = false) => { const sequence = ++navigationSequence.current; setPackageBusy(true); try { const view = community ? { listing: await api.hubListing(id) } : { resource: await api.hubDetail(id) }; if (sequence === navigationSequence.current) setPackageView(view) } catch (e) { if (sequence === navigationSequence.current) fail(e) } finally { if (sequence === navigationSequence.current) setPackageBusy(false) } }
  const closePackage = () => { ++navigationSequence.current; setPackageBusy(false); setPackageView(null) }
  useEffect(() => { closePackage() }, [module, center, mineTab])
  const openShare = (row: api.HubResource) => { setShareRow(row); setRecipient(undefined); setAllowFork(false); setExpires(''); api.hubUsers().then(setUsers).catch(fail) }
  const myRows = mine.filter(r => (!origin || r.origin === origin) && (!keyword || (r.title+' '+r.summary).includes(keyword)))
  const systemRows = system.filter(r => !keyword || (r.title+' '+r.summary).toLowerCase().includes(keyword.toLowerCase()))
  const cards = (rows: api.HubResource[], personal = false) => rows.length ? <div className="hub-grid">{rows.map(row => <article key={row.id} className="hub-resource">
    <header><div className="hub-resource-icon">{row.kind === 'package' ? <AppstoreOutlined /> : <ToolOutlined />}</div><Tag>{row.kind === 'package' ? '技能包' : '独立技能'}</Tag>{personal && <Tag>{originNames[row.origin]}</Tag>}</header>
    <h3>{row.kind === 'package' ? <button className="hub-title-button" onClick={() => void openPackage(row.id)}>{row.title}</button> : row.title}</h3><p className="hub-summary">{row.summary || '暂无简介'}</p>
    <div className="hub-resource-meta">{row.source_title ? `派生自 ${row.source_title} · ${(row.source_version_id || '').slice(0, 8)}` : row.author}</div>
    {personal && <Space wrap><Tag color={row.published_version_id ? 'success' : 'default'}>{row.published_version_id ? '已发布自用版本' : '尚不可用'}</Tag>{row.draft_version_id && <Tag color="processing">有草稿</Tag>}{row.archived && <Tag>已归档</Tag>}{row.blocked && <Tag color="error">安全停用</Tag>}{row.listing_status && <Tag>{statusNames[row.listing_status]}</Tag>}{row.update_available && <Tag color="warning">上游有更新</Tag>}</Space>}
    <footer>{personal ? <Space wrap><Button size="small" onClick={() => void openRead(row.id)}>查看详情 / 预览</Button><Button size="small" onClick={() => setEditorId(row.id)}>编辑与版本</Button><Button size="small" disabled={!row.published_version_id || row.blocked} onClick={() => openShare(row)}>分享</Button><Button size="small" disabled={!row.published_version_id || row.blocked} onClick={() => setSubmitRow(row)}>投稿 / 更新</Button>{row.listing_status === 'published' && <Popconfirm title="主动下架社区条目？已安装副本保留。" onConfirm={() => act(() => api.hubAuthorOffline(row.id, '作者主动下架'))}><Button size="small">社区下架</Button></Popconfirm>}
      {row.update_available && <Popconfirm title="确认升级？有个人修改时将另建新版副本，不覆盖原稿。" onConfirm={() => act(async () => { const next = await api.hubUpgrade(row.id); openMine(next.resource); message.success(next.mode === 'new_copy' ? '已另建新版副本，原修改保留' : '升级完成') })}><Button size="small">确认升级</Button></Popconfirm>}
      <Popconfirm title={row.archived ? '恢复此资源？' : '归档后不能直接运行，文件和版本保留。'} onConfirm={() => act(() => api.hubArchive(row.id, !row.archived))}><Button size="small">{row.archived ? '恢复' : '归档'}</Button></Popconfirm>
      {row.installed_from && <Popconfirm title="卸载到归档区？版本与历史评价保留。" onConfirm={() => act(() => api.hubUninstall(row.id))}><Button size="small">卸载</Button></Popconfirm>}
    </Space> : <Space><Button onClick={() => void openRead(row.id)}>查看详情</Button><Button icon={<CopyOutlined />} disabled={!row.allow_fork} loading={busy} onClick={() => void act(async () => { const forked = await api.hubFork(row.id); openMine(forked); message.success('已创建个人草稿，不影响源技能') })}>{row.allow_fork ? '创建个人副本' : '未开放派生'}</Button></Space>}</footer>
  </article>)}</div> : <Empty description={personal ? '还没有个人技能，创建、导入或从技能中心派生一份。' : '暂无符合条件的系统技能'} />
  return <div className="skill-square-page hub-page">
    <header className="hub-header"><div><h1>Skill 广场</h1><p>在技能中心发现能力，在我的技能中独立创作与管理。</p></div><Button icon={<ReloadOutlined />} onClick={() => void load()} loading={loading}>刷新</Button></header>
    <Tabs className="hub-main-tabs" activeKey={module} onChange={setModule} items={[{ key: 'center', label: <Space><AppstoreOutlined />技能中心</Space> }, { key: 'mine', label: <Space><UserOutlined />我的技能</Space> }]} />
    {error && <Alert type="error" showIcon message={error} action={<Button onClick={() => void load()}>重试</Button>} />}
    {module === 'center' ? <>
      <Tabs activeKey={center} onChange={setCenter} items={[{ key: 'system', label: '系统技能' }, { key: 'community', label: '社区技能' }]} />
      <div className="hub-toolbar"><Input prefix={<SearchOutlined />} allowClear placeholder={center === 'system' ? '搜索系统技能' : '搜索标题、简介或标签'} value={keyword} onChange={e => setKeyword(e.target.value)} />{center === 'community' && <><Select allowClear placeholder="全部分类" value={category || undefined} onChange={v => setCategory(v || '')} options={categories.map(x => ({ label: x, value: x }))} /><Input allowClear placeholder="按标签筛选" value={tag} onChange={e => setTag(e.target.value)} /><Select value={sort} onChange={setSort} options={[{ label: '最新发布', value: 'latest' }, { label: '安装人数', value: 'popular' }, { label: '综合评分', value: 'rating' }]} /></>}</div>
      <Spin spinning={loading || packageBusy}>{packageView ? <PackageCards view={packageView} onBack={closePackage} onPreview={(scope) => { if (packageView.resource) void openRead(packageView.resource.id, scope); else if (packageView.listing) { setCommunityScope(scope); setCommunityId(packageView.listing.id) } }} /> : center === 'system' ? cards(systemRows) : listings.length ? <div className="hub-grid">{listings.map(row => <article className="hub-resource" key={row.id}><header><div className="hub-resource-icon"><ToolOutlined /></div><Tag>{row.kind === 'package' ? '技能包' : '独立技能'}</Tag><Tag>{row.details.category}</Tag></header><h3>{row.kind === 'package' ? <button className="hub-title-button" onClick={() => void openPackage(row.id, true)}>{row.details.title}</button> : row.details.title}</h3><p className="hub-summary">{row.details.summary}</p><div className="hub-resource-meta">{row.author}</div><Space><Rate disabled allowHalf value={row.rating_avg} /><span>{row.rating_count} 条评价</span></Space><footer><span>{row.install_count} 人安装</span><Button type="primary" onClick={() => { setCommunityScope(''); setCommunityId(row.id) }}>{row.installed_resource_id ? '已安装 · 查看' : '详情与安装'}</Button></footer></article>)}</div> : <Empty description="社区暂未有符合条件的已审核技能，你可以从我的技能提交第一份作品。" />}</Spin>
    </> : <>
      <SkillEvolutionPanel onPublished={() => void load()} /><div className="hub-toolbar"><Input allowClear prefix={<SearchOutlined />} placeholder="搜索我的技能" value={keyword} onChange={e => setKeyword(e.target.value)} /><Select allowClear placeholder="全部来源" value={origin || undefined} onChange={v => setOrigin(v || '')} options={Object.entries(originNames).filter(([k]) => k !== 'system').map(([value, label]) => ({ value, label }))} /><Button icon={<UploadOutlined />} onClick={() => setImportOpen(true)}>导入技能</Button><Button type="primary" icon={<PlusOutlined />} onClick={() => setCreateOpen(true)}>创建技能</Button></div>
      <Tabs activeKey={mineTab} onChange={setMineTab} items={[
        { key: 'all', label: `全部技能 (${mine.length})`, children: <Spin spinning={loading || packageBusy}>{packageView ? <PackageCards view={packageView} onBack={closePackage} onPreview={scope => { if (packageView.resource) void openRead(packageView.resource.id, scope) }} /> : cards(myRows, true)}</Spin> },
        { key: 'incoming', label: '分享给我', children: <Table rowKey="id" dataSource={incoming} columns={[{ title: '技能', dataIndex: 'title' }, { title: '作者', dataIndex: 'author' }, { title: '状态', render: (_, r) => <Tag color={r.active ? 'success' : 'default'}>{r.active ? '有效分享' : '已撤销 / 到期'}</Tag> }, { title: '版本', render: (_, r) => r.version_id.slice(0, 8) }, { title: '操作', render: (_, r) => <Space wrap><Button disabled={!r.active} onClick={() => void openRead(r.resource_id)}>查看</Button>{r.allow_fork && <Button disabled={!r.active} onClick={() => void act(async () => openMine(await api.hubFork(r.resource_id)))}>派生副本</Button>}{r.proposed_version_id && <Popconfirm title="确认切换分享版本？定时任务需另外更新。" onConfirm={() => act(() => api.hubAccept(r.id))}><Button disabled={!r.active}>接受新版</Button></Popconfirm>}</Space> }]} /> },
        { key: 'outgoing', label: '我的分享', children: <><Alert showIcon type="info" message="允许派生后，对方可保留独立副本。撤销分享只收回直接访问和使用权限。" /><Table rowKey="id" dataSource={outgoing} columns={[{ title: '技能', dataIndex: 'title' }, { title: '接收人', dataIndex: 'recipient' }, { title: '状态', render: (_, r) => r.active ? '有效' : '已失效' }, { title: '到期时间', render: (_, r) => r.expires_at || '长期有效' }, { title: '派生', render: (_, r) => r.allow_fork ? '允许' : '不允许' }, { title: '操作', render: (_, r) => <Space><Button onClick={() => { const resource = mine.find(x => x.id === r.resource_id); if (resource) openShare(resource) }}>更新分享</Button><Popconfirm title="撤销直接访问？独立副本保留。" onConfirm={() => act(() => api.hubRevoke(r.id))}><Button danger disabled={!r.active}>撤销</Button></Popconfirm></Space> }]} /></> },
        { key: 'publications', label: '我的发布', children: <><Alert type="info" showIcon message="首次上架和后续更新都需审核，新版待审时社区旧版继续可用。" /><Table rowKey="id" dataSource={publications} columns={[{ title: '投稿', render: (_, r) => r.details.title }, { title: '版本', render: (_, r) => r.version_id.slice(0, 8) }, { title: '状态', render: (_, r) => <Tag>{statusNames[r.status] || r.status}</Tag> }, { title: '审核意见', dataIndex: 'review_note' }, { title: '操作', render: (_, r) => <Space><Button onClick={() => setEditorId(r.resource_id)}>打开技能</Button>{r.status === 'pending_review' && <Button onClick={() => void act(() => api.hubWithdraw(r.id))}>撤回申请</Button>}</Space> }]} /></> },
      ]} />
    </>}
    <ResourceEditor id={editorId} onClose={() => setEditorId(null)} onChange={() => void load()} onShare={openShare} onSubmit={setSubmitRow} />
    <CommunityDetail scope={communityScope} id={communityId} onClose={() => setCommunityId(null)} onInstalled={openMine} />
    <Modal title="创建个人技能" open={createOpen} onCancel={() => setCreateOpen(false)} onOk={() => createForm.submit()} confirmLoading={busy} okText="创建草稿"><Form form={createForm} layout="vertical" initialValues={{ content: '# 我的技能\n\n## 目标\n描述此技能要完成的工作。\n\n## 工作步骤\n说明输入、处理步骤与预期输出。' }} onFinish={values => void act(async () => { const row = await api.hubCreate(values); setCreateOpen(false); createForm.resetFields(); openMine(row) })}><Form.Item name="title" label="技能名称" rules={[{ required: true }]}><Input maxLength={200} /></Form.Item><Form.Item name="summary" label="简介"><Input.TextArea rows={2} /></Form.Item><Form.Item name="content" label="SKILL.md" rules={[{ required: true }]}><Input.TextArea rows={8} /></Form.Item></Form></Modal>
    <Modal title="导入到个人空间" open={importOpen} onCancel={() => setImportOpen(false)} onOk={() => importForm.submit()} confirmLoading={busy} okText="导入草稿"><Alert type="info" message="完整ZIP会保留脚本、参考文件和公共资源；多技能自动识别为整包。" /><Form form={importForm} layout="vertical" onFinish={values => void act(async () => { if (!importFile && !folderFiles.length) throw new Error('请选择ZIP或文件夹'); const row = folderFiles.length ? await api.hubImportFolder(folderFiles, values.title, values.summary || '') : await api.hubImport(importFile!, values.title, values.summary || ''); setImportOpen(false); setImportFile(null); setFolderFiles([]); openMine(row) })}><Form.Item name="title" label="名称" rules={[{ required: true }]}><Input /></Form.Item><Form.Item name="summary" label="简介"><Input /></Form.Item><label className="hub-upload-button"><UploadOutlined />选择ZIP<input type="file" accept=".zip" onChange={e => { setImportFile(e.target.files?.[0] || null); setFolderFiles([]) }} /></label><label className="hub-upload-button"><UploadOutlined />选择文件夹<input type="file" multiple {...{ webkitdirectory: '', directory: '' }} onChange={e => { setFolderFiles(Array.from(e.target.files || [])); setImportFile(null) }} /></label><p>{folderFiles.length ? `已选择文件夹：${folderFiles.length}个文件` : importFile?.name || '未选择文件'}</p></Form></Modal>
    <Modal title={`分享 ${shareRow?.title || ''}`} open={!!shareRow} onCancel={() => setShareRow(null)} confirmLoading={busy} okText="发送固定版本分享" onOk={() => { if (!shareRow || !recipient) return message.warning('请选择接收用户'); void act(async () => { await api.hubShare(shareRow.id, { shared_to: recipient, allow_fork: allowFork, expires_at: expires ? new Date(expires).toISOString() : undefined }); setShareRow(null); message.success('分享已发送') }) }}><Alert type="warning" showIcon message="允许派生后，撤销分享不能收回已复制的独立副本。" /><Space direction="vertical" className="hub-full"><Select showSearch placeholder="接收用户" value={recipient} onChange={setRecipient} optionFilterProp="label" options={users.map(u => ({ label: u.name, value: u.id }))} /><Checkbox checked={allowFork} onChange={e => setAllowFork(e.target.checked)}>允许接收者派生独立副本</Checkbox><label>到期时间（留空长期有效）<Input type="datetime-local" value={expires} onChange={e => setExpires(e.target.value)} /></label></Space></Modal>
    <Drawer title="申请社区上架 / 更新" open={!!submitRow} onClose={() => setSubmitRow(null)} width="min(680px, 96vw)" destroyOnClose><Alert type="info" message="提交固定自用版本，首次和更新都需审核。修改草稿不会改变这次申请。" />{submitRow && <DetailsForm action="提交审核" busy={busy} value={submitRow.versions?.find(v => v.id === submitRow.published_version_id)?.metadata || blankDetails(submitRow.title, submitRow.summary)} onSave={d => void act(async () => { await api.hubSubmit(submitRow.id, d); setSubmitRow(null); message.success('已提交审核') })} />}</Drawer>
    <Drawer title={readRow?.title} open={!!readRow} onClose={() => { ++detailSequence.current; setReadRow(null) }} width="min(860px, 96vw)" destroyOnClose><Typography.Paragraph>{readRow?.summary}</Typography.Paragraph><Tag>{readRow?.kind === 'package' ? '整包资源' : '独立技能'}</Tag>{readRow && <SkillFileBrowser key={`${readRow.id}:${readRow.selected_version_id}:${readScope}`} files={readRow.files || []} scope={readScope} canPreview={!!readRow.can_preview} loadFile={path => api.hubFile(readRow.id, readRow.selected_version_id!, path)} />}</Drawer>
  </div>
}
