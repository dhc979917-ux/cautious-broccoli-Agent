import { useEffect, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { Alert, Button, DatePicker, Drawer, Input, InputNumber, Select, Space, Table, Tag, Typography } from 'antd'
import * as api from '../../api/operations'
import { readableError } from '../../api/governance'
import '../Governance/index.css'

export default function GovernanceLogs({ kind }: { kind: 'audit' | 'trace' }) {
  const [search] = useSearchParams()
  const [auditRows, setAuditRows] = useState<api.AuditRow[]>([])
  const [traceRows, setTraceRows] = useState<api.TraceRow[]>([])
  const [detail, setDetail] = useState<api.TraceDetail | null>(null)
  const [page, setPage] = useState(1)
  const [total, setTotal] = useState(0)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [note, setNote] = useState('只展示脱敏元数据，不提供提示词、员工正文或工具参数导出。')
  const [filters, setFilters] = useState<Record<string, string | number | undefined>>({ request_id: search.get('request_id') || undefined, trace_id: search.get('trace_id') || undefined, result: search.get('result') || undefined })
  async function perform(action: () => Promise<unknown>) {
    setBusy(true); setError('')
    try { await action() } catch (cause) { setError(readableError(cause)) } finally { setBusy(false) }
  }
  async function load() {
    if (kind === 'audit') { const response = await api.audits({ ...filters, page }); setAuditRows(response.items); setTotal(response.total); setNote(response.note) }
    else { const response = await api.traces({ ...filters, page }); setTraceRows(response.items); setTotal(response.total); setNote(response.samples.truncated ? '当前扫描达到上限，请缩小时间范围。' : '基于留存OTel样本；缺字段不补造历史版本。') }
  }
  useEffect(() => { void perform(load) }, [kind, page, filters])
  function filter(key: string, value: string | number | undefined) { setFilters((previous) => ({ ...previous, [key]: value })); setPage(1) }
  return <main className="governance-page">
    <header className="governance-heading"><div><Link to="/admin">返回管理后台</Link><Typography.Title level={2}>{kind === 'audit' ? '管理审计' : '调用链检索'}</Typography.Title>
      <Typography.Text type="secondary">Asia/Shanghai · 默认查询今日；最长90天。历史缺样本与查询范围会明确显示。</Typography.Text></div>
      <Button loading={busy} onClick={() => void perform(load)}>刷新</Button></header>
    {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 16 }} />}
    <Alert type="info" message={note} style={{ marginBottom: 20 }} />
    <Space wrap style={{ marginBottom: 20 }}>
      <DatePicker.RangePicker showTime onChange={(values) => { filter('since', values?.[0]?.toISOString()); filter('until', values?.[1]?.toISOString()) }} />
      <Input.Search placeholder="trace_id" defaultValue={filters.trace_id} onSearch={(value) => filter('trace_id', value || undefined)} />
      <Select aria-label="筛选结果" allowClear placeholder="全部结果" style={{ width: 160 }} onChange={(value) => filter('result', value)} options={(kind === 'audit' ? ['succeeded', 'failed', 'denied', 'attempt'] : ['ok', 'error']).map((value) => ({ value, label: value }))} />
      {kind === 'audit' && <><Input.Search placeholder="request_id" defaultValue={filters.request_id} onSearch={(value) => filter('request_id', value || undefined)} />
        <Input.Search placeholder="动作（如 publish_agents）" onSearch={(value) => filter('action', value || undefined)} />
        <Input.Search placeholder="资源类别（如 memories）" onSearch={(value) => filter('resource_type', value || undefined)} />
        <InputNumber aria-label="操作者账号ID" placeholder="操作者账号ID" min={1} onChange={(value) => filter('actor_id', value ?? undefined)} /></>}
    </Space>
    {kind === 'audit' ? <Table rowKey="id" dataSource={auditRows} loading={busy} scroll={{ x: 950 }} pagination={{ current: page, total, pageSize: 20, onChange: setPage }} columns={[
      { title: '时间', dataIndex: 'created_at' }, { title: '操作者账号ID', dataIndex: 'actor_id' }, { title: '动作', dataIndex: 'action' },
      { title: '资源', render: (_, row) => `${row.resource_type} / ${row.resource_id ?? '集合'}` }, { title: '结果', dataIndex: 'result', render: (value) => <Tag>{value}</Tag> },
      { title: 'request_id', dataIndex: 'request_id' }, { title: '调用链', render: (_, row) => row.trace_id ? <Link to={`/admin/traces?trace_id=${encodeURIComponent(row.trace_id)}`}>查看Trace</Link> : '无关联样本' },
    ]} /> : <Table rowKey="trace_id" dataSource={traceRows} loading={busy} scroll={{ x: 850 }} pagination={{ current: page, total, pageSize: 20, onChange: setPage }} columns={[
      { title: '调用链', dataIndex: 'trace_id', render: (value) => <Button type="link" onClick={() => void perform(async () => setDetail(await api.traceDetail(value, filters)))}>{value}</Button> },
      { title: '时间', dataIndex: 'started_at' }, { title: '账号ID', dataIndex: 'user_id', render: (value) => value ?? '历史属主未知' },
      { title: '模型调用', dataIndex: 'model_calls' }, { title: 'Tool调用', dataIndex: 'tool_calls' }, { title: '结果', dataIndex: 'status' },
    ]} />}
    <Drawer title="安全调用链详情" width="min(1000px, 96vw)" open={!!detail} onClose={() => setDetail(null)}>{detail && <>
      <Alert type="info" message={detail.note} /><Typography.Title level={4}>用户 → Agent版本 → 有效模型 → Skill版本 → Tool</Typography.Title>
      <pre className="governance-result">{JSON.stringify(detail.metadata, null, 2)}</pre>
      <Table rowKey="span_id" dataSource={detail.spans} pagination={false} scroll={{ x: 600 }} columns={[
        { title: '阶段', dataIndex: 'kind' }, { title: '父Span', dataIndex: 'parent_span_id' }, { title: '耗时ms', dataIndex: 'duration_ms' }, { title: '结果', dataIndex: 'status' },
        { title: '安全元数据', render: (_, row) => <pre className="governance-result">{JSON.stringify(row.attributes, null, 2)}</pre> },
      ]} /></>}</Drawer>
  </main>
}
