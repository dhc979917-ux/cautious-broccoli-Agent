import { useEffect, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { Alert, Button, DatePicker, Empty, Input, InputNumber, Modal, Select, Space, Table, Tabs, Tag, Typography } from 'antd'
import * as api from '../../api/operations'
import { readableError } from '../../api/governance'
import { hasPerm } from '../../constants/permissions'
import { useAuthStore } from '../../store/authStore'
import '../Governance/index.css'

export default function Operations() {
  const user = useAuthStore((state) => state.user)
  const [search, setSearch] = useSearchParams()
  const [data, setData] = useState<api.Dashboard | null>(null)
  const [pending, setPending] = useState<Awaited<ReturnType<typeof api.pending>> | null>(null)
  const [policies, setPolicies] = useState<api.Policy[]>([])
  const [cleanup, setCleanup] = useState<api.Cleanup[]>([])
  const [gatewayTests, setGatewayTests] = useState<api.GatewayTest[]>([])
  const [editing, setEditing] = useState<api.Policy | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [range, setRange] = useState<{ since?: string; until?: string }>({})
  const [resource, setResource] = useState('trace')
  const [cutoff, setCutoff] = useState('')
  const [reason, setReason] = useState('')
  const tab = search.get('tab') || 'overview'
  async function perform(action: () => Promise<unknown>) {
    setBusy(true); setError('')
    try { await action() } catch (cause) { setError(readableError(cause)) } finally { setBusy(false) }
  }
  async function load() {
    if (tab === 'overview') setData(await api.dashboard(range))
    if (tab === 'pending') setPending(await api.pending())
    if (tab === 'policies') setPolicies(await api.policies())
    if (tab === 'cleanup') setCleanup(await api.cleanupList())
    if (tab === 'gateway') setGatewayTests(await api.gatewayTests())
  }
  useEffect(() => { void perform(load) }, [tab, range])
  function review(row: api.Cleanup, decision: string) {
    Modal.confirm({ title: decision === 'approved' ? '批准留存清理？' : '驳回清理申请？', content: '必须由另一审核人处理。Trace移入受保护归档；审计只清理IP与差异细节，保留事件证据。',
      onOk: () => perform(async () => { await api.reviewCleanup(row.id, { revision: row.revision, decision, reason }); await load() }) })
  }
  return <main className="governance-page">
    <header className="governance-heading"><div><Link to="/admin">返回管理后台</Link><Typography.Title level={2}>运营与治理</Typography.Title>
      <Typography.Text type="secondary">真实用量、待处理、告警责任人和受控留存。统计时区 Asia/Shanghai。</Typography.Text></div><Button loading={busy} onClick={() => void perform(load)}>刷新数据</Button></header>
    {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 16 }} />}
    <Tabs activeKey={tab} onChange={(value) => setSearch({ tab: value })} items={[
      { key: 'overview', label: '今日运营', children: <>
        <DatePicker.RangePicker showTime onChange={(values) => setRange(values ? { since: values[0]?.toISOString(), until: values[1]?.toISOString() } : {})} />
        {!data ? <Empty description={busy ? '读取真实样本中' : '尚无运营数据'} /> : <>
          <Typography.Paragraph type="secondary" style={{ marginTop: 16 }}>范围：{data.since} 至 {data.until} · 更新：{data.updated_at}</Typography.Paragraph>
          <Alert type={data.samples.truncated ? 'warning' : 'info'} message={data.note} description={data.usage.cost_note} />
          <section className="governance-summary" aria-label="今日调用与用量">
            <span>Agent回复 <strong>{data.usage.agent_replies}</strong></span><span>活跃账号 <strong>{data.usage.active_users}</strong></span>
            <span>模型调用 <strong>{data.usage.model_calls}</strong></span><span>Tool调用 <strong>{data.usage.tool_calls}</strong></span>
            <span>输入 / 输出Token <strong>{data.usage.tokens.input} / {data.usage.tokens.output}</strong></span>
          </section><Typography.Paragraph type="secondary">完整用量样本 {data.usage.tokens.complete_samples}；缺用量样本 {data.usage.tokens.missing_samples}；失败率 {data.usage.failure_rate ?? '无样本'}。</Typography.Paragraph>
          <Typography.Title level={4}>健康与异常</Typography.Title>
          <Table rowKey="code" dataSource={data.alerts} pagination={false} scroll={{ x: 600 }} columns={[
            { title: '指标', dataIndex: 'code', render: (code) => api.labels[code] || code },
            { title: '样本值', dataIndex: 'value', render: (value, row) => <Tag color={row.active ? 'error' : 'default'}>{value ?? '未采样'}</Tag> },
            { title: '阈值', dataIndex: 'threshold' }, { title: '责任人账号ID', dataIndex: 'responsible_user_id', render: (value) => value ?? '待指定' },
            { title: '处置入口', render: (_, row) => <Link to={row.href}>查看资源</Link> },
          ]} /><Typography.Title level={4}>最近变更</Typography.Title>
          <Table rowKey="id" dataSource={data.recent} pagination={false} scroll={{ x: 650 }} columns={[
            { title: '动作', dataIndex: 'action' }, { title: '资源', dataIndex: 'resource_type' }, { title: '结果', dataIndex: 'result' },
            { title: '请求', render: (_, row) => <Link to={`/admin/audit?request_id=${encodeURIComponent(row.request_id)}`}>{row.request_id}</Link> },
          ]} />
        </>}
      </> },
      { key: 'pending', label: '待处理', children: <><Alert type="info" message={pending?.note || '只展示有权访问的资源；入口会再次鉴权。'} />
        <Table rowKey={(row) => `${row.type}-${row.id}`} loading={busy} dataSource={pending?.items || []} scroll={{ x: 600 }} columns={[
          { title: '类别', dataIndex: 'type' }, { title: '资源ID', dataIndex: 'id' }, { title: '属主账号ID', dataIndex: 'owner_id' },
          { title: '状态', dataIndex: 'status' }, { title: '处理', render: (_, row) => <Link to={row.href}>进入工作台</Link> },
        ]} /></> },
      { key: 'gateway', label: '模型测试与故障', children: <><Alert type="info" message="来自受限模型测试记录；不展示测试Prompt或输出正文。错误码保留，不用红色计数代替诊断。" />
        <Table rowKey="id" dataSource={gatewayTests} scroll={{ x: 750 }} columns={[
          { title: '模型', dataIndex: 'model_id' }, { title: '操作者账号ID', dataIndex: 'actor_id' },
          { title: '结果', dataIndex: 'outcome' }, { title: '错误码', dataIndex: 'error_code' },
          { title: '耗时ms', dataIndex: 'duration_ms' }, { title: '测试标识', dataIndex: 'trace_id' }, { title: '时间', dataIndex: 'tested_at' },
        ]} /></> },
      ...(hasPerm(user, 'ops_manage.policy') ? [{ key: 'policies', label: '告警与留存政策', children: <>
        <Alert type="info" message="阈值只触发告警；保留期到期不会自动删除。责任人未配置时明确显示待指定。" />
        <Table rowKey="resource" dataSource={policies} scroll={{ x: 650 }} columns={[
          { title: '资源 / 指标', dataIndex: 'resource', render: (value) => api.labels[value] || value },
          { title: '保留天数', dataIndex: 'retention_days' }, { title: '告警阈值', dataIndex: 'backlog_threshold' },
          { title: '责任人账号ID', dataIndex: 'responsible_user_id', render: (value) => value ?? '待指定' },
          { title: '操作', render: (_, row) => <Button onClick={() => setEditing(row)}>编辑政策</Button> },
        ]} /></> }] : []),
      ...(hasPerm(user, 'ops_manage.cleanup') ? [{ key: 'cleanup', label: '清理审批', children: <>
        <Alert type="warning" message="已发布记忆在记忆工作台逐项审批。失败/驳回候选可审批清除正文；已发布来源快照不清除。" />
        <Space wrap style={{ margin: '18px 0' }}><Select value={resource} onChange={setResource} options={['trace', 'audit', 'candidate_memory'].map((value) => ({ value, label: api.labels[value] }))} />
          <DatePicker showTime placeholder="留存清理截止时间" onChange={(value) => setCutoff(value?.toISOString() || '')} />
          <Input placeholder="申请 / 审核原因（至少10字）" value={reason} onChange={(event) => setReason(event.target.value)} style={{ width: 320 }} />
          <Button disabled={!cutoff || reason.trim().length < 10} onClick={() => void perform(async () => { await api.requestCleanup({ resource, cutoff, reason }); await load() })}>申请清理</Button></Space>
        <Table rowKey="id" dataSource={cleanup} scroll={{ x: 750 }} columns={[
          { title: '资源', dataIndex: 'resource' }, { title: '对象数', dataIndex: 'target_count' }, { title: '状态', dataIndex: 'status' },
          { title: '申请 / 审核人', render: (_, row) => `${row.requested_by} / ${row.reviewed_by ?? '待审'}` },
          { title: '操作', render: (_, row) => <Space wrap>{row.status === 'pending' && <>
            <Button disabled={reason.length < 10 || row.requested_by === user?.id} onClick={() => review(row, 'approved')}>批准</Button>
            <Button disabled={reason.length < 10 || row.requested_by === user?.id} onClick={() => review(row, 'rejected')}>驳回</Button></>}
            {row.status === 'approved' && <Button onClick={() => Modal.confirm({ title: '执行已审批清理？', onOk: () => perform(async () => { await api.executeCleanup(row.id); await load() }) })}>执行受控清理</Button>}</Space> },
        ]} /></> }] : []),
    ]} />
    <Modal title="编辑告警与留存政策" open={!!editing} onCancel={() => setEditing(null)} onOk={() => void perform(async () => {
      if (editing) await api.savePolicy(editing); setEditing(null); await load()
    })}>{editing && <Space direction="vertical">
      <label>保留天数 <InputNumber min={1} max={3650} value={editing.retention_days} onChange={(value) => setEditing({ ...editing, retention_days: value ?? 365 })} /></label>
      <label>告警阈值 <InputNumber min={1} max={10000} value={editing.backlog_threshold} onChange={(value) => setEditing({ ...editing, backlog_threshold: value ?? 100 })} /></label>
      <label>责任人账号 ID <InputNumber min={1} value={editing.responsible_user_id} onChange={(value) => setEditing({ ...editing, responsible_user_id: value })} /></label>
    </Space>}</Modal>
  </main>
}
