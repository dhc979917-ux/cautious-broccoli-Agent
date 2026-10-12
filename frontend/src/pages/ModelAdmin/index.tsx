import { useCallback, useEffect, useRef, useState } from 'react'
import axios from 'axios'
import { Alert, Button, Descriptions, Drawer, Empty, Form, Input, InputNumber, Result, Select, Space, Switch, Table, Tabs, Tag, Tooltip, Typography, message } from 'antd'
import { InfoCircleOutlined, ReloadOutlined } from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'
import { changeModelAuthorization, getModelCatalog, saveModelMetadata, type ModelCatalogItem, type ModelCatalogResponse, type ModelMetadataInput } from '../../api/model'
import { useAuthStore } from '../../store/authStore'
import { invalidateModelDirectory } from '../../store/modelDirectory'
import { ROLE_LABELS } from '../../constants/roles'
import ModelAuthorization from './ModelAuthorization'
import './index.css'

function failureText(error: unknown): string {
  if (axios.isAxiosError(error)) {
    if (error.response?.status === 409) return '数据已被其他人修改，请刷新后重试。'
    const detail = error.response?.data?.detail
    if (typeof detail === 'string') return detail
  }
  return '请求失败，请稍后重试。'
}

function timeLabel(value: string | null): string {
  return value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '暂无记录'
}

const purposes = [{ value: 'chat', label: 'Chat' }, { value: 'vision', label: '视觉' },
  { value: 'embedding', label: '向量' }, { value: 'other', label: '其他' }]

export default function ModelAdmin() {
  const isAdmin = useAuthStore((state) => state.user?.role === 'admin')
  const [catalog, setCatalog] = useState<ModelCatalogResponse | null>(null)
  const [page, setPage] = useState(1)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [current, setCurrent] = useState<ModelCatalogItem | null>(null)
  const [tab, setTab] = useState('metadata')
  const [submitting, setSubmitting] = useState(false)
  const [roleSaving, setRoleSaving] = useState<string | null>(null)
  const [form] = Form.useForm()
  const requestSequence = useRef(0)
  const load = useCallback(async (refresh = false) => {
    const sequence = ++requestSequence.current
    setLoading(true); setError('')
    try {
      const next = await getModelCatalog(page, refresh)
      if (sequence === requestSequence.current) setCatalog(next)
    } catch (cause) { if (sequence === requestSequence.current) setError(failureText(cause)) }
    finally { if (sequence === requestSequence.current) setLoading(false) }
  }, [page])
  useEffect(() => { if (isAdmin) void load(); return () => { requestSequence.current += 1 } }, [isAdmin, load])
  function select(item: ModelCatalogItem) {
    setCurrent(item); setTab('metadata')
    form.setFieldsValue({ display_name: item.display_name, purpose: item.purpose || 'chat',
      context_length: item.context_length, enabled: item.enabled,
      input_price_per_million: item.input_price_per_million,
      output_price_per_million: item.output_price_per_million,
      cached_input_price_per_million: item.cached_input_price_per_million })
  }
  async function save(values: ModelMetadataInput) {
    if (!current) return
    setSubmitting(true)
    try {
      const revision = await saveModelMetadata({ ...values, model_id: current.model_id, revision: current.revision,
        display_name: values.display_name?.trim() || null,
        context_length: values.context_length ?? null,
        input_price_per_million: values.input_price_per_million || null,
        output_price_per_million: values.output_price_per_million || null,
        cached_input_price_per_million: values.cached_input_price_per_million || null })
      setCurrent((previous) => previous?.model_id === current.model_id ? { ...previous, ...values, revision } : previous)
      invalidateModelDirectory(); await load()
      message.success('模型元数据已保存')
    } catch (cause) { message.error(failureText(cause)) } finally { setSubmitting(false) }
  }
  async function assign(item: ModelCatalogItem, role: string) {
    setRoleSaving(item.model_id)
    try {
      await changeModelAuthorization(item.model_id, 'minimum_role', role, true)
      invalidateModelDirectory(); await load()
      message.success('最低可用角色已更新')
    } catch (cause) { message.error(failureText(cause)) } finally { setRoleSaving(null) }
  }
  const columns: ColumnsType<ModelCatalogItem> = [
    { title: '网关模型', key: 'model', align: 'center', ellipsis: true, render: (_, item) =>
      <Tooltip title={item.model_id}><Button type="link" className="model-name-link" onClick={() => select(item)}>
        {item.display_name || item.model_id}</Button></Tooltip> },
    { title: '用途', dataIndex: 'purpose', align: 'center', render: (value) => purposes.find((item) => item.value === value)?.label || 'Chat' },
    { title: '目录 / 平台', key: 'status', align: 'center', render: (_, item) => <Space size={4} wrap>
      <Tag>{item.in_gateway === false ? '目录已移除' : item.in_gateway === null ? '缓存目录' : '目录可用'}</Tag>
      <Tag color={item.enabled ? 'success' : 'default'}>{item.enabled ? '启用' : '停用'}</Tag></Space> },
    { title: '最低可用角色', key: 'role', align: 'center', render: (_, item) => <Select
      aria-label={`${item.model_id}最低可用角色`} value={item.minimum_role} loading={roleSaving === item.model_id}
      disabled={!!roleSaving} style={{ minWidth: 116 }} onChange={(value) => void assign(item, value)}
      options={[...['viewer', 'operator', 'admin'].map((value) => ({ value, label: ROLE_LABELS[value] })),
        ...(item.minimum_role === 'custom' ? [{ value: 'custom', label: '自定义', disabled: true }] : [])]} /> },
  ]
  if (!isAdmin) return <Result status="403" title="无权访问模型治理" subTitle="需要管理员权限" />
  const status = catalog?.gateway_reachable === false ? '刷新失败，展示近期缓存' : catalog?.gateway_reachable ? '已实时获取网关目录' : '展示近期缓存'
  return <main className="model-governance-page">
    <header className="model-governance-header"><div><div className="model-governance-title-row">
      <Typography.Title level={3}>企业模型网关</Typography.Title>
      {catalog && <Tooltip title="点击实时刷新可重新探测网关"><span className="model-cache-badge" role="status">
        <InfoCircleOutlined />{status} · 最后成功获取：{timeLabel(catalog.fetched_at)}</span></Tooltip>}
    </div><Typography.Text type="secondary">统一管理企业模型的展示信息、平台状态与授权范围。</Typography.Text></div>
      <Button icon={<ReloadOutlined />} loading={loading} onClick={() => void load(true)}>实时刷新</Button></header>
    {error && <Alert type="error" showIcon message={error} action={<Button onClick={() => void load()}>重试</Button>} />}
    <Table rowKey="model_id" loading={loading} columns={columns} dataSource={catalog?.items || []}
      tableLayout="auto" locale={{ emptyText: <Empty description="暂无网关模型" /> }}
      pagination={{ current: page, pageSize: 8, total: catalog?.total || 0, showSizeChanger: false,
        showTotal: (total) => `共 ${total} 个模型`, onChange: setPage }} />
    <Drawer title={current?.display_name || current?.model_id || '模型设置'} open={!!current}
      onClose={() => setCurrent(null)} width="min(720px, 100vw)" className="model-governance-drawer">
      {current && <Tabs activeKey={tab} onChange={setTab} items={[
        { key: 'metadata', label: '元数据与健康', children: <>
          <Descriptions size="small" column={1} className="model-metadata-identifiers" items={[
            { key: 'id', label: '网关 ID', children: current.model_id },
            { key: 'provider', label: 'Provider', children: current.provider || '—' },
            { key: 'seen', label: '上次发现', children: timeLabel(current.last_seen_at) },
          ]} />
          <Form form={form} layout="vertical" onFinish={save} className="model-metadata-form">
            <Form.Item name="display_name" label="显示名"><Input maxLength={200} placeholder="留空使用网关模型 ID" /></Form.Item>
            <div className="model-metadata-grid"><Form.Item name="purpose" label="用途"><Select options={purposes} /></Form.Item>
              <Form.Item name="context_length" label="上下文长度（Token）"><InputNumber min={1} max={10000000} precision={0} style={{ width: '100%' }} /></Form.Item></div>
            <div className="model-governance-prices">
              <Form.Item name="input_price_per_million" label="输入价格"><Input inputMode="decimal" placeholder="可不填" /></Form.Item>
              <Form.Item name="output_price_per_million" label="输出价格"><Input inputMode="decimal" placeholder="可不填" /></Form.Item>
              <Form.Item name="cached_input_price_per_million" label="命中缓存价格"><Input inputMode="decimal" placeholder="可不填" /></Form.Item>
            </div><Typography.Text type="secondary" className="model-price-unit">价格单位：USD / 百万 Token；留空表示未设置。</Typography.Text>
            <div className="model-metadata-footer"><Space><Typography.Text>平台允许使用</Typography.Text>
              <Form.Item name="enabled" valuePropName="checked" noStyle><Switch checkedChildren="启用" unCheckedChildren="停用" /></Form.Item></Space>
              <Button type="primary" htmlType="submit" loading={submitting}>保存元数据</Button></div>
          </Form>
        </> },
        { key: 'access', label: '授权范围', children: <ModelAuthorization key={current.model_id} modelId={current.model_id} onChanged={load} /> },
      ]} />}
    </Drawer>
  </main>
}
