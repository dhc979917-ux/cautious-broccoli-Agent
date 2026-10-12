import { useEffect, useRef, useState } from 'react'
import { Alert, Button, Drawer, Empty, Input, List, message, Rate, Space, Tabs, Tag, Typography } from 'antd'
import SkillFileBrowser from '../../../components/SkillFileBrowser'
import * as api from '../../../api/skillHub'
import { useAuthStore } from '../../../store/authStore'
const fail = (e: any) => message.error(e?.response?.data?.detail || e.message || '操作失败')
export default function CommunityDetail({ id, scope = '', onClose, onInstalled }: { id: string | null; scope?: string; onClose: () => void; onInstalled: (r: api.HubResource) => void }) {
  const [row, setRow] = useState<api.HubListing | null>(null)
  const [reviews, setReviews] = useState<api.HubReview[]>([])
  const [busy, setBusy] = useState(false)
  const [rating, setRating] = useState(5)
  const [comment, setComment] = useState('')
  const [note, setNote] = useState('')
  const user = useAuthStore(s => s.user)
  const sequence = useRef(0)
  const load = async () => { if (!id) return; const request = ++sequence.current; const [a, b] = await Promise.all([api.hubListing(id), api.hubReviews(id)]); if (request === sequence.current) { setRow(a); setReviews(b) } }
  useEffect(() => { setRow(null); setRating(5); setComment(''); setNote(''); if (id) void load().catch(fail); return () => { ++sequence.current } }, [id])
  const act = async (task: () => Promise<unknown>) => { setBusy(true); try { await task(); await load() } catch (e) { fail(e) } finally { setBusy(false) } }
  return <Drawer open={!!id} onClose={onClose} width="min(860px, 96vw)" title={row?.details.title || '社区技能'} destroyOnClose>
    {!row ? <Empty description="正在加载" /> : <>
      <Space wrap><Tag>{row.kind === 'package' ? '技能包' : '独立技能'}</Tag><Tag>{row.details.category}</Tag><span>{row.author}</span><Rate disabled value={row.rating_avg} allowHalf /><span>{row.rating_count} 条评价 · {row.install_count} 人安装</span></Space>
      <Typography.Paragraph className="hub-detail-summary">{row.details.summary}</Typography.Paragraph>
      <Alert type="info" showIcon message="安装后获得独立个人副本。作者更新不会自动替换你的内容，也不会附赠工具和模型权限。" />
      <Space className="hub-editor-toolbar"><Button type="primary" loading={busy} onClick={() => void act(async () => { const installed = await api.hubInstall(row.id); onInstalled(installed); message.success(installed.published_version_id ? '已安装并建立自用版本' : '已安装，依赖或测试环境需配置后才能使用') })}>{row.installed_resource_id ? '打开我的副本' : '安装到我的技能'}</Button>{row.installed_resource_id && <Button onClick={() => void act(async () => onInstalled(await api.hubInstall(row.id, true)))}>再创建副本</Button>}</Space>
      <Tabs key={`${id}:${scope}`} defaultActiveKey={scope ? 'files' : 'intro'} items={[
        { key: 'intro', label: '介绍与使用', children: <>{[['详细说明', row.details.description], ['使用示例', row.details.example], ['使用前提', row.details.requirements], ['风险说明', row.details.risks], ['来源与许可', row.details.license]].map(([label, value]) => <section key={label}><Typography.Title level={5}>{label}</Typography.Title><Typography.Paragraph style={{ whiteSpace: 'pre-wrap' }}>{value || '未填写'}</Typography.Paragraph></section>)}<Typography.Title level={5}>检查报告</Typography.Title><pre className="hub-report">{JSON.stringify(row.test_report, null, 2)}</pre></> },
        { key: 'files', label: '批准版本文件', children: <SkillFileBrowser key={`${row.id}:${row.version_id}:${scope}`} files={row.files || []} scope={scope} canPreview={!!row.can_preview} loadFile={path => api.hubCommunityFile(row.id, path, row.version_id)} /> },
        { key: 'history', label: '社区版本历史', children: <List dataSource={row.history || []} renderItem={v => <List.Item><div><Tag>{v.version_id === row.version_id ? '当前社区版本' : '历史批准版本'}</Tag><Typography.Text code>{v.version_id.slice(0, 8)}</Typography.Text><p>{v.details.summary}</p><span>{v.reviewed_at}</span></div></List.Item>} /> },
        { key: 'reviews', label: `评价 (${reviews.length})`, children: <><List dataSource={reviews} renderItem={r => <List.Item><div className="hub-review"><Space><strong>{r.author}</strong><Rate disabled value={r.rating} /><Tag>版本 {r.version_id.slice(0, 8)}</Tag></Space><p>{r.comment}</p>{r.author_reply && <Alert message={`作者回复：${r.author_reply}`} />}{r.user_id === user?.id && <Button size="small" danger onClick={() => void act(() => api.hubDeleteReview(r.id))}>删除我的评价</Button>}{row.author_id === user?.id && <Space><Input placeholder="回复评价" onChange={e => setNote(e.target.value)} /><Button onClick={() => void act(() => api.hubReply(r.id, note))}>回复</Button></Space>}</div></List.Item>} />
          {row.author_id !== user?.id && <><Typography.Title level={5}>我的评价</Typography.Title><Rate value={rating} onChange={setRating} /><Input.TextArea rows={3} value={comment} onChange={e => setComment(e.target.value)} maxLength={4000} placeholder="安装体验及适用场景" /><Button className="hub-editor-toolbar" loading={busy} onClick={() => void act(async () => { await api.hubReview(row.id, rating, comment); message.success('评价已保存') })}>提交 / 更新评价</Button></>}
        </> },
        { key: 'report', label: '举报问题', children: <><Alert type="warning" message="安全问题与评分分开处理，请说明风险及复现信息。" /><Input.TextArea rows={4} value={note} onChange={e => setNote(e.target.value)} maxLength={4000} /><Button loading={busy} disabled={!note.trim()} onClick={() => void act(async () => { await api.hubReport(row.id, note); message.success('举报已提交') })}>提交举报</Button></> },
      ]} />
    </>}
  </Drawer>
}
