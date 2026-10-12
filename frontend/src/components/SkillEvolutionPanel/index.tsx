import { useCallback, useEffect, useRef, useState } from 'react'
import { Alert, Button, Drawer, Empty, List, message, Modal, Select, Space, Spin, Switch, Tag, Typography } from 'antd'
import * as api from '../../api/skillEvolution'
import './index.css'

const status: Record<string, string> = { queued: '等待复盘', evaluating: '评估中', validating: '验证中', checking: '验证中', ready: '准备发布', no_change: '无需修改', published: '已自用发布', awaiting_confirmation: '待确认', awaiting_target: '待选择副本', failed: '验证失败', conflict: '版本冲突', cancelled: '已取消' }
const busyStates = new Set(['queued', 'evaluating', 'validating', 'checking', 'ready'])
const fail = (e: any) => e?.response?.data?.detail || '加载失败，请稍后重试'

interface Props { resourceId?: string; sessionId?: number; compact?: boolean; onPublished?: () => void }
export default function SkillEvolutionPanel({ resourceId, sessionId, compact = false, onPublished }: Props) {
  const [preference, setPreference] = useState<api.EvolutionPreference | null>(null)
  const [jobs, setJobs] = useState<api.EvolutionJob[]>([])
  const [loading, setLoading] = useState(true)
  const [acting, setActing] = useState(false)
  const [error, setError] = useState('')
  const [showUnchanged, setShowUnchanged] = useState(false)
  const [selected, setSelected] = useState<api.EvolutionJob | null>(null)
  const [diffs, setDiffs] = useState<{ path: string; diff: string }[]>([])
  const [diffLoading, setDiffLoading] = useState(false)
  const observed = useRef(new Map<string, string>())
  const generation = useRef(0)
  const diffGeneration = useRef(0)
  const publishedRef = useRef(onPublished)
  publishedRef.current = onPublished

  const load = useCallback(async () => {
    const current = generation.current
    try {
      const [prefs, list] = await Promise.all([api.evolutionPreference(), api.evolutionJobs(resourceId, sessionId)])
      if (current !== generation.current) return
      for (const job of list) {
        if (job.status === 'published' && observed.current.has(job.id) && observed.current.get(job.id) !== 'published') publishedRef.current?.()
        observed.current.set(job.id, job.status)
      }
      setPreference(prefs); setJobs(list); setError('')
      setSelected(current => current ? list.find(job => job.id === current.id) || current : null)
    } catch (e) { if (current === generation.current) setError(fail(e)) } finally { if (current === generation.current) setLoading(false) }
  }, [resourceId, sessionId])
  useEffect(() => {
    let active = true
    ++generation.current
    ++diffGeneration.current
    observed.current.clear()
    setSelected(null); setJobs([])
    setLoading(true)
    void load()
    const timer = window.setInterval(() => { if (active && !document.hidden) void load() }, 6000)
    return () => { active = false; ++generation.current; ++diffGeneration.current; window.clearInterval(timer) }
  }, [load])

  const act = async (task: () => Promise<unknown>) => {
    setActing(true)
    try { await task(); await load() } catch (e) { message.error(fail(e)) } finally { setActing(false) }
  }
  const enable = (enabled: boolean) => {
    if (!enabled) { void act(() => api.setEvolutionEnabled(false)); return }
    Modal.confirm({ title: '开启个人技能自动进化？', content: '仅复盘你完成的问答，可能产生额外模型费用。需要修改时自动派生或修复个人技能；验证通过的低风险版本自动自用发布，高风险变更等你确认。不会修改系统技能、自动分享或公开到社区。', okText: '开启自动进化', cancelText: '暂不开启', onOk: () => act(() => api.setEvolutionEnabled(true)) })
  }
  const open = async (job: api.EvolutionJob) => {
    const current = ++diffGeneration.current
    setSelected(job); setDiffs([]); setDiffLoading(true)
    try { const values = await api.evolutionDiff(job.id); if (current === diffGeneration.current) setDiffs(values) } catch (e) { if (current === diffGeneration.current) message.error(fail(e)) } finally { if (current === diffGeneration.current) setDiffLoading(false) }
  }
  const visible = jobs.filter(job => showUnchanged || job.status !== 'no_change')
  const paused = !!resourceId && !!preference?.paused_resources.includes(resourceId)
  return <section className={`skill-evolution${compact ? ' skill-evolution-compact' : ''}`} aria-label="个人技能进化">
    <div className="skill-evolution-head"><div><Typography.Text strong>个人技能进化</Typography.Text><p>有证据才改，旧版本始终可追溯。</p></div>
      <Space><Switch aria-label={resourceId ? '此技能自动进化' : '个人技能自动进化'} checked={resourceId ? !!preference?.enabled && !paused : !!preference?.enabled} disabled={loading || acting} onChange={value => resourceId && preference?.enabled ? void act(() => api.evolutionPause(resourceId, !value)) : enable(value)} /><Button size="small" onClick={() => void load()} loading={loading}>刷新</Button></Space>
    </div>
    {error && <Alert type="error" showIcon message={error} action={<Button size="small" onClick={() => void load()}>重试</Button>} />}
    {!error && !preference?.enabled && !loading && <Typography.Paragraph type="secondary">默认关闭。开启后仅自动发布验证充分的低风险个人版本，不影响定时任务。</Typography.Paragraph>}
    <Spin spinning={loading}><List size="small" dataSource={visible.slice(0, compact ? 3 : 12)} locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无进化记录，正常使用问答即可" /> }} renderItem={job => <List.Item actions={[<Button key="detail" size="small" type="link" onClick={() => void open(job)}>查看变更</Button>]}><List.Item.Meta title={<Space wrap><Tag color={job.status === 'published' ? 'success' : job.status === 'failed' || job.status === 'conflict' ? 'error' : busyStates.has(job.status) ? 'processing' : 'default'}>{status[job.status] || job.status}</Tag><span>{new Date(job.created_at).toLocaleString('zh-CN')}</span></Space>} description={job.reason || '正在检查是否有可复用的改进'} /></List.Item>} /></Spin>
    {compact && visible.length > 3 && <Typography.Paragraph type="secondary">更多记录请在 Skill 广场 → 我的技能查看。</Typography.Paragraph>}
    {jobs.some(job => job.status === 'no_change') && <Button type="link" size="small" onClick={() => setShowUnchanged(!showUnchanged)}>{showUnchanged ? '隐藏无需修改记录' : '查看无需修改记录'}</Button>}
    <Drawer title="技能进化记录" width="min(840px, 96vw)" open={!!selected} onClose={() => { ++diffGeneration.current; setSelected(null) }}>
      {selected && <><Space wrap><Tag>{status[selected.status] || selected.status}</Tag><Typography.Text>{selected.reason}</Typography.Text></Space>
        {!!selected.report.risks?.length && <Alert type="warning" showIcon message="需要人工确认的风险" description={selected.report.risks.join('；')} />}
        {selected.status === 'awaiting_target' && <Select aria-label="选择个人副本" placeholder="选择同来源的个人副本" options={selected.report.choices?.map(row => ({ value: row.id, label: row.title }))} onChange={id => void act(() => api.evolutionChooseTarget(selected.id, id))} style={{ width: '100%' }} />}
        <Space wrap className="skill-evolution-actions">
          {selected.status === 'awaiting_confirmation' && <Button type="primary" loading={acting} onClick={() => Modal.confirm({ title: '确认高风险候选并自用发布？', content: '请先检查下方差异与验证报告。此操作不改变分享版本或社区版本。', okText: '确认并发布', cancelText: '继续检查', onOk: () => act(() => api.evolutionAction(selected.id, 'confirm')) })}>确认并发布</Button>}
          {['failed', 'conflict'].includes(selected.status) && <Button loading={acting} onClick={() => void act(() => api.evolutionAction(selected.id, 'retry'))}>重试验证</Button>}
          {!['published', 'no_change', 'cancelled'].includes(selected.status) && <Button loading={acting} onClick={() => void act(() => api.evolutionAction(selected.id, 'reject'))}>拒绝候选 / 取消</Button>}
          {selected.target_resource_id && <Button onClick={() => void act(() => api.evolutionPause(selected.target_resource_id!, !preference?.paused_resources.includes(selected.target_resource_id!)))}>{preference?.paused_resources.includes(selected.target_resource_id) ? '恢复此技能进化' : '暂停此技能进化'}</Button>}
        </Space>
        <Typography.Title level={5}>文件差异</Typography.Title><Spin spinning={diffLoading}>{diffs.length ? diffs.map(item => <div key={item.path}><Typography.Text strong>{item.path}</Typography.Text><pre className="skill-evolution-code">{item.diff}</pre></div>) : <Typography.Paragraph type="secondary">暂无文件修改；已发布版本的回滚请使用技能版本管理。</Typography.Paragraph>}</Spin>
        <Typography.Title level={5}>验证证据</Typography.Title><pre className="skill-evolution-code">{JSON.stringify(selected.report, null, 2)}</pre>
      </>}
    </Drawer>
  </section>
}
