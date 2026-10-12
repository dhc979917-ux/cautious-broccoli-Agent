import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { Alert, Button, Descriptions, Empty, Form, Input, Modal, Select, Space, Spin, Tabs, Tag, Typography, message } from 'antd'
import { ArrowLeftOutlined, ReloadOutlined } from '@ant-design/icons'
import {
  agentHistory, agentVersionDiff, cloneAgentVersion, readableError, releaseAgent,
  saveAgentDraft, testAgentDraft, type AgentHistory, type AgentRevision,
} from '../../api/governance'
import { useAuthStore } from '../../store/authStore'
import { useModelDirectory } from '../../store/modelDirectory'
import '../Governance/index.css'

const { TextArea } = Input

export default function AgentVersions() {
  const agentId = Number(useParams().agentId)
  const { modelOptions } = useModelDirectory()
  const isAdmin = useAuthStore((state) => state.user?.role === 'admin')
  const [data, setData] = useState<AgentHistory | null>(null)
  const [selectedId, setSelectedId] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [diff, setDiff] = useState<Record<string, { before: unknown; after: unknown }> | null>(null)
  const [testResult, setTestResult] = useState('')
  const [prompt, setPrompt] = useState('请用一句话说明你的职责。')
  const [form] = Form.useForm()
  const selected: AgentRevision | undefined = data?.versions.find((item) => item.id === selectedId)

  async function load(focus?: string) {
    if (!Number.isInteger(agentId) || agentId <= 0) return
    setBusy(true)
    setError('')
    try {
      const next = await agentHistory(agentId)
      setData(next)
      setSelectedId((previous) => focus || (next.versions.some((item) => item.id === previous) ? previous : next.published_version_id || next.versions[0]?.id || ''))
    } catch (cause) {
      setError(readableError(cause))
    } finally {
      setBusy(false)
    }
  }

  useEffect(() => { if (isAdmin) void load() }, [agentId, isAdmin])
  useEffect(() => {
    if (!selected) return
    const snapshot = selected.snapshot
    form.setFieldsValue({ name: snapshot.name, description: snapshot.description,
      agent_type: snapshot.agent_type, system_prompt: snapshot.system_prompt,
      model_config_name: snapshot.model_config_name,
      memory_policy: snapshot.memory_policy || 'stage6_disabled',
      mcp_servers: (snapshot.config.mcp_servers || []).join(', '),
      knowledge_bases: (snapshot.config.knowledge_bases || []).join(', '),
      builtin_skills: (snapshot.config.builtin_skills || []).join(', '),
      max_react_iters: snapshot.config.max_react_iters || undefined,
      tool_grants: JSON.stringify(snapshot.tool_grants || [], null, 2) })
    setDiff(null)
    setTestResult('')
  }, [selectedId, selected?.revision])

  async function run(action: () => Promise<unknown>, success: string, focus?: string) {
    setBusy(true)
    setError('')
    try {
      const result = await action()
      message.success(success)
      await load(focus || (result as AgentRevision)?.id)
    } catch (cause) {
      setError(readableError(cause))
    } finally {
      setBusy(false)
    }
  }

  function parseIds(input: string): number[] {
    const values = String(input || '').split(',').map((value) => value.trim()).filter(Boolean)
    if (values.some((value) => !/^\d+$/.test(value))) throw Error('资源 ID 请使用逗号分隔的整数')
    return values.map(Number)
  }

  async function save(values: Record<string, string | number>) {
    if (!selected) return
    try {
      const toolGrants = JSON.parse(String(values.tool_grants || '[]'))
      if (!Array.isArray(toolGrants)) throw Error('Tool 白名单必须是数组')
      await run(() => saveAgentDraft(agentId, selected.id, selected.revision, {
        name: values.name, description: values.description || null,
        agent_type: values.agent_type, system_prompt: values.system_prompt || null,
        model_config_name: values.model_config_name,
        memory_policy: values.memory_policy,
        config: {
          mcp_servers: parseIds(String(values.mcp_servers || '')),
          knowledge_bases: parseIds(String(values.knowledge_bases || '')),
          builtin_skills: String(values.builtin_skills || '').split(',').map((value) => value.trim()).filter(Boolean),
          ...(values.max_react_iters ? { max_react_iters: Number(values.max_react_iters) } : {}),
          _skill_versions: selected.snapshot.config._skill_versions || {},
        },
        tool_grants: toolGrants,
      }), '草稿已保存', selected.id)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '配置格式无效')
    }
  }

  function confirmRelease(rollback: boolean) {
    if (!selected || !data) return
    Modal.confirm({ title: rollback ? '将新会话回退到此版本？' : '发布并影响后续新会话？',
      content: '进行中的会话固定原版本；依赖与授权会在服务端再次校验。',
      onOk: async () => { await run(() => releaseAgent(agentId, selected.id, data.revision, rollback), rollback ? '回滚成功' : '发布成功', selected.id) } })
  }

  if (!isAdmin) return <div className="governance-page"><Alert type="error" message="无权查看 Agent 版本" showIcon /></div>
  if (!Number.isInteger(agentId) || agentId <= 0) return <div className="governance-page"><Empty description="无效的 Agent ID" /></div>

  return <main className="governance-page">
    <header className="governance-heading"><div><Link to="/admin/agents"><ArrowLeftOutlined /> 返回 Agent 管理</Link>
      <Typography.Title level={2}>Agent 发布工作台</Typography.Title>
      <Typography.Text type="secondary">草稿不影响运行中任务；新会话按发布指针取版本。</Typography.Text></div>
      <Button icon={<ReloadOutlined />} onClick={() => void load()} loading={busy}>刷新</Button></header>
    {error && <Alert type="error" showIcon message={error} closable onClose={() => setError('')} />}
    {busy && !data ? <Spin /> : data && <>
      {!data.versioning_enabled && <Alert showIcon type="warning" message="版本治理尚未切流；旧管理入口仍按现有规则运行" />}
      <div className="governance-summary"><span>Agent #{agentId}</span><span>已发布：{data.published_version_id?.slice(0, 8) || '未发布'}</span>
        <span>历史记录：{data.history.length}</span></div>
      <div className="governance-select"><Select value={selectedId || undefined} onChange={setSelectedId} style={{ minWidth: 300 }}
        options={data.versions.map((item) => ({ value: item.id, label: `v${item.number} · ${item.state === 'published' ? '已发布' : '草稿'} · ${item.id.slice(0, 8)}` }))} />
        {selected && <Tag color={selected.state === 'published' ? 'green' : 'gold'}>{selected.test_status}</Tag>}
        {selected && <Button onClick={() => void run(() => cloneAgentVersion(agentId, selected.id), '新草稿已创建')} disabled={busy}>复制为草稿</Button>}
        {selected && data.published_version_id !== selected.id && <Button onClick={() => void agentVersionDiff(agentId, selected.id).then((result) => setDiff(result.fields)).catch((cause) => setError(readableError(cause)))}>查看变更</Button>}
        {selected?.state === 'published' && data.published_version_id !== selected.id && <Button danger onClick={() => confirmRelease(true)} disabled={!data.versioning_enabled}>回滚至此</Button>}
        {selected?.state === 'draft' && <Button type="primary" onClick={() => confirmRelease(false)} disabled={!data.versioning_enabled || selected.test_status !== 'passed'}>发布草稿</Button>}
      </div>
      {diff && <Alert type="info" showIcon message={`本次变化：${Object.keys(diff).join('、') || '无'}`} description="Prompt 内容仅在已授权详情页查看，操作审计不记录正文。" />}
      {selected ? <Tabs items={[
        { key: 'edit', label: '配置与依赖', children: <Form form={form} layout="vertical" disabled={selected.state !== 'draft' || !data.versioning_enabled} onFinish={(values) => void save(values)}>
          <div className="governance-fields"><Form.Item name="name" label="名称" rules={[{ required: true }]}><Input maxLength={100} /></Form.Item>
            <Form.Item name="agent_type" label="架构"><Select options={[{ value: 'dialog', label: '工具助手' }, { value: 'task', label: '数据分析师' }, { value: 'rag', label: 'RAG（沿用现有回退实现）' }]} /></Form.Item>
            <Form.Item name="description" label="描述"><Input /></Form.Item>
            <Form.Item name="model_config_name" label="默认模型" rules={[{ required: true }]}><Select showSearch optionFilterProp="label" options={modelOptions} /></Form.Item></div>
          <Form.Item name="system_prompt" label="System Prompt"><TextArea rows={6} /></Form.Item>
          <div className="governance-fields"><Form.Item name="mcp_servers" label="MCP Server ID（逗号分隔）"><Input /></Form.Item>
            <Form.Item name="knowledge_bases" label="共享知识库 ID（逗号分隔）"><Input /></Form.Item>
            <Form.Item name="builtin_skills" label="附加 Skill 标识（逗号分隔）"><Input /></Form.Item>
            <Form.Item name="max_react_iters" label="最大推理轮数"><Input type="number" min={1} max={100} /></Form.Item></div>
          <Form.Item name="tool_grants" label="Tool 白名单（server_id、tool_name、schema_digest）"><TextArea rows={4} /></Form.Item>
          <Form.Item name="memory_policy" label="长期记忆策略"><Select options={[
            { value: 'stage6_disabled', label: '兼容基线（启用治理时仍须人工审核）' },
            { value: 'candidate_review', label: '仅生成候选，人工审核后发布' },
            { value: 'disabled', label: '禁用长期记忆读写' },
          ]} /></Form.Item>
          <Alert type="info" showIcon message="Skill 版本固定在快照中；记忆候选不参与检索，禁止自动发布。" style={{ marginBottom: 16 }} />
          {selected.state === 'draft' && <Button type="primary" htmlType="submit">保存草稿</Button>}
        </Form> },
        { key: 'test', label: '测试与发布', children: <Space direction="vertical" className="governance-stack">
          <Alert type="warning" showIcon message="模型测试不等于完整 Agent 工具链测试；未通过完整隔离测试的草稿无法发布" />
          <Input value={prompt} onChange={(event) => setPrompt(event.target.value)} maxLength={200} aria-label="测试提问" />
          <Button disabled={selected.state !== 'draft'} loading={busy} onClick={() => void run(async () => {
            const result = await testAgentDraft(agentId, selected.id, prompt)
            setTestResult(JSON.stringify(result, null, 2))
          }, '模型探测完成', selected.id)}>模型探测</Button>
          {testResult && <pre className="governance-result">{testResult}</pre>}
        </Space> },
        { key: 'history', label: '发布记录', children: data.history.length ? <Descriptions column={1} bordered size="small" items={data.history.map((item) => ({ key: item.id, label: item.action, children: `${item.version_id.slice(0, 8)} · ${new Date(item.created_at).toLocaleString()}` }))} /> : <Empty description="暂无发布事件" /> },
      ]} /> : <Empty description="暂无版本，请先完成存量基线迁移" />}
    </>}
  </main>
}
