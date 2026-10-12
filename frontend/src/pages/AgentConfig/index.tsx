import { useEffect, useState } from 'react'
import { useParams } from 'react-router-dom'
import { Alert, Button, Empty, Form, Input, InputNumber, Modal, Result, Select, Space, Spin, Tabs, Tag, message } from 'antd'
import { ReloadOutlined } from '@ant-design/icons'
import { agentHistory, cloneAgentVersion, readableError, releaseAgent, reviewedTools, saveAgentDraft, testAgentDraft, type AgentHistory, type AgentRevision, type ReviewedTool, type ToolGrant } from '../../api/governance'
import { listMcpServers, type McpServerInfo } from '../../api/mcp'
import { listKnowledgeBases, type KnowledgeBaseInfo } from '../../api/knowledge'
import { getAllSkills, type SkillInfo } from '../../api/skill'
import { useModelDirectory } from '../../store/modelDirectory'
import { useAuthStore } from '../../store/authStore'
import './index.css'
import SkillMultiSelect from '../../components/SkillMultiSelect'

type ConfigValues = { name: string; description?: string; model: string; prompt?: string; servers?: number[]; bases?: number[]; skills?: string[]; tools?: string[]; iterations?: number; memory: string }
const toolKey = (serverId: number, name: string) => `${serverId}:${name}`

export default function AgentConfig() {
  const agentId = Number(useParams().agentId)
  const isAdmin = useAuthStore((state) => state.user?.role === 'admin')
  const [data, setData] = useState<AgentHistory | null>(null)
  const [draft, setDraft] = useState<AgentRevision | null>(null)
  const [servers, setServers] = useState<McpServerInfo[]>([])
  const [bases, setBases] = useState<KnowledgeBaseInfo[]>([])
  const [skills, setSkills] = useState<SkillInfo[]>([])
  const { models, modelLabel } = useModelDirectory()
  const [tools, setTools] = useState<(ReviewedTool & { server_id: number })[]>([])
  const [reselectedTools, setReselectedTools] = useState<Set<string>>(new Set())
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [resourceError, setResourceError] = useState('')
  const [toolError, setToolError] = useState('')
  const [dirty, setDirty] = useState(false)
  const [form] = Form.useForm<ConfigValues>()
  const selectedServers: number[] | undefined = Form.useWatch('servers', form)
  const current = draft || data?.versions.find((item) => item.id === data.published_version_id) || data?.versions[0]
  const published = data?.versions.find((item) => item.id === data.published_version_id)

  async function load() {
    setBusy(true); setError('')
    try {
      const next = await agentHistory(agentId)
      setData(next)
      setDraft(next.versions.filter((item) => item.state === 'draft').sort((left, right) => right.number - left.number)[0] || null)
      setDirty(false)
    } catch (cause) { setError(readableError(cause)) } finally { setBusy(false) }
  }
  useEffect(() => {
    if (!isAdmin || !Number.isInteger(agentId) || agentId <= 0) return
    void load()
    Promise.all([listMcpServers(true), listKnowledgeBases(), getAllSkills()]).then(([connections, knowledge, availableSkills]) => {
      setServers(connections); setBases(knowledge.filter((item) => item.scope !== 'personal')); setSkills(availableSkills)
    }).catch((cause) => setResourceError(readableError(cause)))
  }, [agentId, isAdmin])
  useEffect(() => {
    if (!current) return
    setReselectedTools(new Set())
    const snapshot = current.snapshot
    form.setFieldsValue({ name: snapshot.name, description: snapshot.description || '', model: snapshot.model_config_name,
      prompt: snapshot.system_prompt || '', servers: snapshot.config.mcp_servers || [], bases: snapshot.config.knowledge_bases || [],
      skills: snapshot.config.builtin_skills || [], tools: (snapshot.tool_grants || []).map((grant) => toolKey(grant.server_id, grant.tool_name)),
      iterations: snapshot.config.max_react_iters, memory: snapshot.memory_policy || 'stage6_disabled' })
  }, [current?.id, current?.revision])
  useEffect(() => {
    let alive = true
    Promise.all((selectedServers || []).map(async (id) => (await reviewedTools(id)).map((tool) => ({ ...tool, server_id: id })))).then((catalogs) => {
      if (alive) { setTools(catalogs.flat()); setToolError('') }
    }).catch((cause) => { if (alive) setToolError(readableError(cause)) })
    return () => { alive = false }
  }, [JSON.stringify(selectedServers || [])])

  async function save(submitted: ConfigValues) {
    const values = { ...form.getFieldsValue(true), ...submitted } as ConfigValues
    if (!current || !data || resourceError || toolError) return
    setBusy(true); setError('')
    try {
      const grants = (values.tools || []).filter((key) => (values.servers || []).includes(Number(key.split(':')[0]))).map((key): ToolGrant => {
        const tool = tools.find((item) => toolKey(item.server_id, item.name) === key)
        const previous = current.snapshot.tool_grants.find((item) => toolKey(item.server_id, item.tool_name) === key)
        if (previous && !reselectedTools.has(key) && (values.servers || []).includes(previous.server_id)) return previous
        if (!tool || !tool.available || tool.risk === 'unknown') throw Error('所选工具已不可用或尚未审核，请在 MCP 工具页面复核。')
        return { server_id: tool.server_id, tool_name: tool.name, schema_digest: tool.schema_digest }
      }).filter((grant) => (values.servers || []).includes(grant.server_id))
      const candidate = current.state === 'draft' ? current : await cloneAgentVersion(agentId, current.id)
      const saved = await saveAgentDraft(agentId, candidate.id, candidate.revision, { name: values.name, description: values.description || null,
        system_prompt: values.prompt || null, model_config_name: values.model, memory_policy: values.memory,
        config: { ...current.snapshot.config, mcp_servers: values.servers || [], knowledge_bases: values.bases || [], builtin_skills: values.skills || [],
          ...(values.iterations ? { max_react_iters: values.iterations } : { max_react_iters: null }) }, tool_grants: grants })
      await load(); setDraft(saved); setDirty(false); message.success('配置已保存，应用前不会影响运行中的会话。')
    } catch (cause) { setError(cause instanceof Error ? readableError(cause) === '操作失败，请稍后重试' ? cause.message : readableError(cause) : readableError(cause)) }
    finally { setBusy(false) }
  }
  async function check() {
    if (!draft || dirty) return
    setBusy(true); setError('')
    try { await testAgentDraft(agentId, draft.id, 'Do not use real business tools. Reply with OK.'); await load(); message.success('配置检查通过') }
    catch (cause) { setError(readableError(cause)) } finally { setBusy(false) }
  }
  function apply() {
    if (!draft || !data || dirty) return
    Modal.confirm({ title: '应用这次配置修改？', content: '新会话将使用新配置；正在执行的会话保持原配置。', okText: '应用配置', cancelText: '取消', onOk: async () => {
      setBusy(true)
      try { await releaseAgent(agentId, draft.id, data.revision); await load(); message.success('配置已应用') }
      catch (cause) { setError(readableError(cause)); throw cause } finally { setBusy(false) }
    } })
  }
  if (!isAdmin) return <Result status="403" title="无权管理 Agent 配置" />
  if (!Number.isInteger(agentId) || agentId <= 0) return <Empty description="无效的 Agent" />
  return <section className="agent-config">
    {error && <Alert type="error" showIcon message={error} />}
    {resourceError && <Alert type="warning" showIcon message={`依赖目录暂不可用：${resourceError}`} />}
    {toolError && <Alert type="warning" showIcon message={`工具目录暂不可用：${toolError}`} />}
    {!current && busy ? <Spin /> : current && <Form form={form} layout="vertical" onValuesChange={() => setDirty(true)} onFinish={(values) => void save(values)} disabled={busy || !data?.versioning_enabled}>
      <div className="agent-config-state"><div><strong>当前生效配置</strong><span>{published ? `${published.snapshot.name} · ${modelLabel(published.snapshot.model_config_name)}` : '尚未应用；请完成配置后检查并应用'}</span></div><Space><Tag color={draft ? draft.test_status === 'failed' ? 'error' : 'warning' : 'success'}>{draft ? draft.test_status === 'passed' ? '检查通过，待应用' : draft.test_status === 'failed' ? '检查失败，尚未生效' : '尚未应用' : '已生效'}</Tag><Button icon={<ReloadOutlined />} aria-label="刷新配置" onClick={() => void load()} /></Space></div>
      {draft?.test_summary && draft.test_status === 'failed' && <Alert type="error" showIcon message={draft.test_summary} />}
      <Tabs items={[
        { key: 'basic', label: '基本配置', children: <div className="agent-config-form"><div className="agent-config-fields">
          <Form.Item name="name" label="名称" rules={[{ required: true }]}><Input maxLength={100} /></Form.Item>
          <Form.Item name="model" label="默认模型" rules={[{ required: true }]}><Select showSearch optionFilterProp="label" options={[...new Set([...models, current.snapshot.model_config_name])].map((modelId) => ({ value: modelId, label: modelLabel(modelId) }))} /></Form.Item>
          <Form.Item name="iterations" label="最大推理轮数"><InputNumber min={1} max={100} placeholder="不限制" style={{ width: '100%' }} /></Form.Item></div>
          <div className="agent-config-texts"><Form.Item name="description" label="说明"><Input.TextArea rows={5} /></Form.Item>
          <Form.Item name="prompt" label="系统提示词"><Input.TextArea rows={5} /></Form.Item></div>
          <Form.Item name="memory" label="长期记忆" tooltip="不自动提取：可使用已有记忆，不从聊天自动提取；已禁用：不使用个人长期记忆；提取后等待人工审核：自动生成候选，审核发布后才可使用。应用配置后，新会话生效，已有会话保留原策略。"><Select options={[{ value: 'stage6_disabled', label: '不自动提取' }, { value: 'disabled', label: '已禁用' }, { value: 'candidate_review', label: '提取后等待人工审核' }]} /></Form.Item>
        </div> },
        { key: 'resources', label: '资源绑定', children: <div className="agent-config-form agent-config-resources">
          <Form.Item name="servers" label="MCP 服务" extra="停用服务保留绑定但不参与执行；软删除服务不可重新绑定。"><Select mode="multiple" showSearch optionFilterProp="label" options={servers.filter((item) => !item.is_deleted).map((item) => ({ value: item.id, label: item.enabled ? item.name : `${item.name}（已停用）`, disabled: !item.enabled }))} /></Form.Item>
          <Form.Item name="tools" label="允许调用的工具" extra="新增工具不会自动授权。参数结构变化时，取消后重新勾选并保存可更新授权；高风险工具调用仍须确认。"><Select mode="multiple" showSearch optionFilterProp="label" onSelect={(key: string) => setReselectedTools((previous) => new Set([...previous, key]))}
            options={tools.map((item) => ({ value: toolKey(item.server_id, item.name), label: `${servers.find((server) => server.id === item.server_id)?.name || item.server_id} / ${item.name}${item.risk === 'unknown' ? '（未审核）' : ''}`, disabled: !item.available || item.risk === 'unknown' }))} /></Form.Item>
          <Form.Item name="bases" label="共享知识库"><Select mode="multiple" showSearch optionFilterProp="label" options={bases.map((item) => ({ value: item.id, label: item.name }))} /></Form.Item>
          <Form.Item name="skills" label="内置技能" extra="在架构自带技能之外，勾选后随此助手自动加载；技能包可展开逐项选择，也可整包勾选。">
            <SkillMultiSelect skills={skills.filter((item) => item.is_active && item.has_access && !item.dir_missing)} disabled={busy} />
          </Form.Item>
        </div> },
      ]} />
      <footer className="agent-config-footer"><span>保存 → 检查 → 应用；历史会话保留助手配置，技能使用当前发布版。</span><Space wrap>
        <Button htmlType="submit" disabled={!dirty || !!resourceError || !!toolError}>保存修改</Button><Button onClick={() => void check()} disabled={!draft || dirty}>检查配置</Button>
        <Button type="primary" onClick={apply} disabled={!draft || draft.test_status !== 'passed' || dirty}>应用配置</Button></Space></footer>
    </Form>}
  </section>
}
