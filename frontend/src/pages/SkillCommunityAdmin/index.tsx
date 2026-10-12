import { useEffect, useState } from 'react'
import { Alert, Button, Checkbox, Drawer, Input, List, message, Modal, Select, Space, Table, Tabs, Tag } from 'antd'
import * as api from '../../api/skillHub'
import { useAuthStore } from '../../store/authStore'
import '../SkillSquare/hub.css'
const fail = (e: any) => message.error(e?.response?.data?.detail || e.message || '操作失败')
export default function SkillCommunityAdmin() {
  const user = useAuthStore(s => s.user)
  const [reviews, setReviews] = useState<api.HubModerationReview[]>([])
  const [requests, setRequests] = useState<api.HubRequest[]>([])
  const [listings, setListings] = useState<api.HubListing[]>([])
  const [resources, setResources] = useState<api.HubResource[]>([])
  const [reports, setReports] = useState<api.HubReportItem[]>([])
  const [selected, setSelected] = useState<api.HubRequest | null>(null)
  const [preview, setPreview] = useState<api.FilePreview | null>(null)
  const [changes, setChanges] = useState<api.FileChange[]>([])
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [status, setStatus] = useState('pending_review')
  const [operation, setOperation] = useState<{ kind: 'offline' | 'block' | 'resolve'; id: string } | null>(null)
  const [version, setVersion] = useState('')
  const [descendants, setDescendants] = useState(false)
  const load = async () => { const [a, b, c, d, e] = await Promise.all([api.hubRequests(status), api.hubAdminListings(), api.hubAdminResources(), api.hubReports(), api.hubModerationReviews()]); setRequests(a); setListings(b); setResources(c); setReports(d); setReviews(e) }
  useEffect(() => { if (user?.role === 'admin') void load().catch(fail) }, [status, user?.role])
  const act = async (task: () => Promise<unknown>) => { setBusy(true); try { await task(); await load() } catch (e) { fail(e) } finally { setBusy(false) } }
  const inspect = (r: api.HubRequest) => { setSelected(r); setPreview(null); setNote(''); api.hubRequestDiff(r.id).then(setChanges).catch(fail) }
  const startOperation = (kind: 'offline' | 'block' | 'resolve', id: string) => { setOperation({ kind, id }); setNote(''); setVersion(''); setDescendants(false) }
  if (user?.role !== 'admin') return <Alert type="error" message="仅管理员可进入社区审核" />
  return <div className="hub-admin"><h1>技能社区审核</h1><p>审核固定投稿版本；个人私有草稿不在这里展示。</p><Tabs items={[
    { key: 'requests', label: '投稿与更新审核', children: <><Space className="hub-editor-toolbar"><Select value={status} onChange={setStatus} options={[{ label: '待审核', value: 'pending_review' }, { label: '已通过', value: 'approved' }, { label: '已驳回', value: 'rejected' }, { label: '全部申请', value: '' }]} /><Button onClick={() => void load().catch(fail)}>刷新</Button></Space><Table rowKey="id" dataSource={requests} columns={[{ title: '投稿', render: (_, r) => r.details.title }, { title: '作者', dataIndex: 'author' }, { title: '类型', render: (_, r) => r.current_version_id ? '社区更新' : '首次上架' }, { title: '状态', dataIndex: 'status' }, { title: '版本', render: (_, r) => r.version_id.slice(0, 8) }, { title: '操作', render: (_, r) => <Button onClick={() => inspect(r)}>查看提交快照</Button> }]} /></> },
    { key: 'listings', label: '条目治理', children: <Table rowKey="id" dataSource={listings} columns={[{ title: '条目', render: (_, r) => r.details.title || '未上架条目' }, { title: '作者', dataIndex: 'author' }, { title: '状态', dataIndex: 'status' }, { title: '安装人数', dataIndex: 'install_count' }, { title: '治理', render: (_, r) => <Space><Button onClick={() => startOperation('offline', r.id)}>普通下架</Button><Button danger onClick={() => startOperation('block', r.resource_id)}>安全停用</Button></Space> }]} /> },
    { key: 'resources', label: '资源状态', children: <><Alert type="info" showIcon message="管理员只能查看个人资源归属和状态，不能通过此入口浏览或改写私有内容。派生许可请在 Skill 管理的操作→编辑中设置。" /><Table rowKey="id" dataSource={resources} columns={[{ title: '资源', dataIndex: 'title' }, { title: '所有者', render: (_, r) => r.owner_id || '平台维护' }, { title: '来源', dataIndex: 'origin' }, { title: '状态', render: (_, r) => r.blocked ? '安全停用' : r.archived ? '已归档' : '正常' }, { title: '操作', render: (_, r) => <Space><Button danger onClick={() => startOperation('block', r.id)}>安全停用</Button></Space> }]} /></> },
    { key: 'reviews', label: '评价管理', children: <Table rowKey="id" dataSource={reviews} columns={[{ title: '条目', dataIndex: 'listing_id' }, { title: '评分', dataIndex: 'rating' }, { title: '评价', dataIndex: 'comment' }, { title: '操作', render: (_, r: api.HubModerationReview) => <Button onClick={() => void act(() => api.hubHideReview(r.id, !r.hidden))}>{r.hidden ? '恢复显示' : '隐藏违规评价'}</Button> }]} /> },
    { key: 'reports', label: '举报处理', children: <Table rowKey="id" dataSource={reports} columns={[{ title: '条目', dataIndex: 'listing_id' }, { title: '问题', dataIndex: 'reason' }, { title: '状态', dataIndex: 'status' }, { title: '处理记录', dataIndex: 'resolution' }, { title: '操作', render: (_, r) => <Button disabled={r.status === 'resolved'} onClick={() => startOperation('resolve', r.id)}>记录处理结果</Button> }]} /> },
  ]} />
  <Drawer open={!!selected} onClose={() => setSelected(null)} title={selected?.details.title} width="min(1000px, 96vw)">
    {selected && <><Alert type="info" message={`固定提交版本 ${selected.version_id}；作者后续草稿不会改变本申请。`} /><Space className="hub-editor-toolbar"><Tag>作者：{selected.author}</Tag><Tag>{selected.status}</Tag></Space><h3>介绍、示例、风险与许可</h3><pre className="hub-report">{JSON.stringify(selected.details, null, 2)}</pre><h3>检查与测试报告</h3><pre className="hub-report">{JSON.stringify(selected.test_report, null, 2)}</pre><h3>与当前社区版本的文件差异</h3><List dataSource={changes} renderItem={c => <List.Item><Tag>{c.action}</Tag>{c.path}</List.Item>} /><h3>提交文件</h3><List dataSource={selected.files} renderItem={f => <List.Item><Button type="link" onClick={() => api.hubRequestFile(selected.id, f.path).then(setPreview).catch(fail)}>{f.path}</Button></List.Item>} />{preview && <pre className="hub-report">{preview.content || '二进制文件'}</pre>}<Input.TextArea aria-label="审核意见" placeholder="审核意见；驳回必须说明原因" rows={3} value={note} onChange={e => setNote(e.target.value)} maxLength={4000} /><Space className="hub-editor-toolbar"><Button type="primary" loading={busy} disabled={selected.status !== 'pending_review' || selected.author_id === user.id} onClick={() => void act(async () => { await api.hubDecision(selected.id, true, note); setSelected(null); message.success('批准版本已上架') })}>审核通过</Button><Button danger loading={busy} disabled={selected.status !== 'pending_review' || !note.trim() || selected.author_id === user.id} onClick={() => void act(async () => { await api.hubDecision(selected.id, false, note); setSelected(null) })}>驳回</Button></Space>{selected.author_id === user.id && <Alert message="不能审核自己的投稿，请其他管理员处理。" />}</>}
  </Drawer>
  <Modal open={!!operation} title={operation?.kind === 'block' ? '安全停用（不删除文件）' : operation?.kind === 'offline' ? '普通下架' : '记录举报处理结果'} onCancel={() => setOperation(null)} confirmLoading={busy} okButtonProps={{ disabled: !note.trim(), danger: operation?.kind === 'block' }} onOk={() => { if (!operation) return; void act(async () => { if (operation.kind === 'offline') await api.hubOffline(operation.id, note); else if (operation.kind === 'resolve') await api.hubResolve(operation.id, note); else await api.hubBlock(operation.id, note, version || undefined, descendants); setOperation(null) }) }}>
    <Alert type="warning" message={operation?.kind === 'block' ? '安全停用会阻止后续执行。请明确版本与派生范围，不能用普通下架代替。' : '普通下架只停止新安装，已有个人副本保留。'} />
    <Input.TextArea rows={4} value={note} onChange={e => setNote(e.target.value)} placeholder="原因及处理说明" maxLength={4000} />
    {operation?.kind === 'block' && <Space direction="vertical" className="hub-full"><Input value={version} onChange={e => setVersion(e.target.value)} placeholder="特定版本ID；留空表示该资源全部版本" /><Checkbox checked={descendants} onChange={e => setDescendants(e.target.checked)}>同时停用从该版本派生的资源链</Checkbox></Space>}
  </Modal>
  </div>
}
