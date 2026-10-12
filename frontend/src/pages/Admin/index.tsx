import { useState, useEffect, useRef } from 'react'
import { Button, Space, Table, Tag, Modal, Form, Input, InputNumber, Select, Radio, Switch, message, Popconfirm, Alert, Descriptions, Tooltip, Spin, Segmented } from 'antd'
import { ArrowLeftOutlined, PlusOutlined, DeleteOutlined, UndoOutlined, EditOutlined, RobotOutlined, ToolOutlined, StopOutlined, ExclamationCircleOutlined, ReloadOutlined, BulbOutlined, DashboardOutlined, UserOutlined, LinkOutlined, PushpinOutlined, SearchOutlined } from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'
import { getAllAgents, getAllUsers, createAgent, deleteAgent, restoreAgent, hardDeleteAgent, getAllSkillsAdmin, createSkill, updateSkill, deleteSkill, restoreSkill, hardDeleteSkill, refreshSkills, getAllPackagesAdmin, updatePackage, deletePackage, restorePackage, hardDeletePackage, changeSkillScope, changePackageScope, SkillPackageInfo, SkillScope } from '../../api/admin'
import { updateAgent } from '../../api/admin'
import { getModelPolicies, setModelPolicy, ModelPolicy } from '../../api/model'
import { useModelDirectory } from '../../store/modelDirectory'
import { ROLE_COLORS, ROLE_LABELS } from '../../constants/roles'
import { hasPerm, hasModulePerm } from '../../constants/permissions'
import { useAuthStore } from '../../store/authStore'
import PlatformLogo from '../../components/PlatformLogo'
import { listUploadJobs, isUploadJobRunning } from '../../api/skill'
import { listMcpServers, McpServerInfo } from '../../api/mcp'
import { listKnowledgeBases, KnowledgeBaseInfo, MEMORY_KB_PREFIX } from '../../api/knowledge'
import { agentVersioning, skillVersioning, createAgentDraft, readableError } from '../../api/governance'
import './index.css'
import SkillResourceUpdate from '../../components/SkillResourceUpdate'
import SkillPackageUpload from '../../components/SkillPackageUpload'

const { TextArea } = Input

// 基底 Agent（工具助手底座/数据分析师底座）：这两个预置 Agent 不可编辑，
// 只有基于基底创建的 Agent 才显示编辑按钮
const BASE_AGENT_NAMES = ['工具助手', '数据分析师']

// 技能归属（与后端 skill_service.SCOPE_* 一致）：可选 / 工具助手内置 / 数据分析师内置
const SCOPE_LABELS: Record<SkillScope, string> = {
  optional: '可选skill',
  dialog: '工具助手skill',
  task: '数据分析师skill',
}

interface Agent { id: number; name: string; description?: string; agent_type: string; model_config_name: string; system_prompt?: string; is_active: boolean; created_at: string; config?: Record<string, any> }
interface Skill { id: number; name: string; display_name: string; description?: string; display_name_zh?: string | null; description_zh?: string | null; required_role: string; is_active: number; is_hidden?: boolean; allow_fork?: boolean; fork_policy_inherited?: boolean; builtin_dialog?: number; builtin_task?: number; package_id?: number | null; rel_path?: string | null; dir_missing?: boolean; scope_locked?: boolean; created_at: string }
// 技能管理表格的混合行：技能包行（可展开）或普通技能行
type SkillTableRow = (Skill & { __pkg?: false; members?: Skill[] }) | (SkillPackageInfo & { __pkg: true; members: Skill[] })

// 表格最小宽度：宽屏时各列按百分比自适应撑满容器，窄屏收缩到该宽度后横向滚动
const TABLE_MIN_WIDTH = 900

export default function Admin({ view }: { view: 'agents' | 'skills' | 'model-permissions' }) {
  const navigate = useNavigate()
  const { user, theme, toggleTheme } = useAuthStore()
  // 模块级：两个管理块各自显隐；按钮再按叶子权限细分（admin 天然全权限）
  const canManageAgent = hasModulePerm(user, 'agent_manage')
  const canManageSkill = hasModulePerm(user, 'skill_manage')
  const canAgentCreate = hasPerm(user, 'agent_manage.create')
  const canAgentUpdate = hasPerm(user, 'agent_manage.update')
  const canAgentDelete = hasPerm(user, 'agent_manage.delete.soft')
  const canAgentRestore = hasPerm(user, 'agent_manage.delete.restore')
  const canAgentHard = hasPerm(user, 'agent_manage.delete.hard')
  const canSkillCreate = hasPerm(user, 'skill_manage.create')
  const canSkillUpdate = hasPerm(user, 'skill_manage.update')
  const canSkillDelete = hasPerm(user, 'skill_manage.delete.soft')
  const canSkillRestore = hasPerm(user, 'skill_manage.delete.restore')
  const canSkillHard = hasPerm(user, 'skill_manage.delete.hard')
  const canSkillRefresh = hasPerm(user, 'skill_manage.refresh')
  const canPkgUpload = hasPerm(user, 'skill_manage.package.upload')
  const canPkgUpdate = hasPerm(user, 'skill_manage.package.update')
  const canPkgDelete = hasPerm(user, 'skill_manage.package.soft')
  const canPkgRestore = hasPerm(user, 'skill_manage.package.restore')
  const canPkgHard = hasPerm(user, 'skill_manage.package.hard')
  const [agents, setAgents] = useState<Agent[]>([])
  const [agentSearch, setAgentSearch] = useState('')
  const [skillSearch, setSkillSearch] = useState('')
  const [agentPage, setAgentPage] = useState(1)
  const [skillPage, setSkillPage] = useState(1)
  const [skills, setSkills] = useState<Skill[]>([])
  const [packages, setPackages] = useState<SkillPackageInfo[]>([])
  const [loading, setLoading] = useState(true)
  const requestSequence = useRef(0)
  const [loadError, setLoadError] = useState('')
  const [expandedPackage, setExpandedPackage] = useState<(SkillPackageInfo & { members: Skill[] }) | null>(null)
  const [versioning, setVersioning] = useState(false)
  const [agentModalOpen, setAgentModalOpen] = useState(false)
  const [agentForm] = Form.useForm()
  // 编辑 Agent（仅非基底 Agent 可编辑：名称/描述/系统提示词）
  const [editAgentModalOpen, setEditAgentModalOpen] = useState(false)
  const [editingAgent, setEditingAgent] = useState<Agent | null>(null)
  const [editAgentForm] = Form.useForm()
  // 基底 Agent 绑定（知识库/MCP）：名称与提示词不可改，只调绑定项
  const [bindingAgent, setBindingAgent] = useState<Agent | null>(null)
  const [bindingModalOpen, setBindingModalOpen] = useState(false)
  const [bindingForm] = Form.useForm()
  const [skillModalOpen, setSkillModalOpen] = useState(false)
  const [editingSkill, setEditingSkill] = useState<Skill | null>(null)
  const [skillForm] = Form.useForm()
  // 归属切换弹窗（内置 / 取消内置）
  const [scopeTarget, setScopeTarget] = useState<SkillTableRow | null>(null)
  const [scopeValue, setScopeValue] = useState<SkillScope>('optional')
  const [scopeSaving, setScopeSaving] = useState(false)
  const [pkgModalOpen, setPkgModalOpen] = useState(false)
  const [editingPkg, setEditingPkg] = useState<SkillPackageInfo | null>(null)
  const [pkgForm] = Form.useForm()
  // 技能分组：可选skill / 工具助手skill(内置) / 数据分析师skill(内置)
  const [skillGroup, setSkillGroup] = useState<'optional' | 'dialog' | 'task'>('optional')
  const agentQuery = agentSearch.trim().toLowerCase()
  const skillQuery = skillSearch.trim().toLowerCase()
  const filteredAgents = agents.filter((agent) =>
    !agentQuery || `${agent.name} ${agent.description || ''} ${agent.agent_type}`.toLowerCase().includes(agentQuery)
  )
  const groupedSkills = skills.filter((s) =>
    (skillGroup === 'dialog' ? s.builtin_dialog === 1
      : skillGroup === 'task' ? s.builtin_task === 1
      : s.builtin_dialog !== 1 && s.builtin_task !== 1)
  )
  const filteredSkills = groupedSkills.filter((s) =>
    (!skillQuery || `${s.name} ${s.display_name} ${s.display_name_zh || ''} ${s.description || ''}`.toLowerCase().includes(skillQuery))
  )
  // 可选组数据源：技能包行（含成员）在前，独立技能行在后
  const optionalSkills = filteredSkills
  const optionalDataSource: SkillTableRow[] = [
    ...packages.map((p) => ({ ...p, __pkg: true as const, members: groupedSkills.filter((s) => s.package_id === p.id) }))
      .filter((p) => p.members.length > 0 || (skillGroup === 'optional' && !skills.some((skill) => skill.package_id === p.id)))
      .filter((p) => !skillQuery || `${p.name} ${p.display_name} ${p.description || ''}`.toLowerCase().includes(skillQuery) || p.members.some((s) => `${s.name} ${s.display_name} ${s.display_name_zh || ''}`.toLowerCase().includes(skillQuery)))
      .map((p) => ({ ...p, members: skillQuery && !`${p.name} ${p.display_name} ${p.description || ''}`.toLowerCase().includes(skillQuery) ? p.members.filter((s) => `${s.name} ${s.display_name} ${s.display_name_zh || ''}`.toLowerCase().includes(skillQuery)) : p.members })),
    ...optionalSkills.filter((s) => !s.package_id && (!skillQuery || `${s.name} ${s.display_name} ${s.display_name_zh || ''} ${s.description || ''}`.toLowerCase().includes(skillQuery))),
  ]
  const agentCurrentPage = Math.min(agentPage, Math.max(1, Math.ceil(filteredAgents.length / 8)))
  const skillTotal = optionalDataSource.length
  const skillPageSize = expandedPackage ? 3 : 8
  const skillCurrentPage = Math.min(skillPage, Math.max(1, Math.ceil(skillTotal / skillPageSize)))
  const agentPagination = { current: agentCurrentPage, pageSize: 8, showSizeChanger: false, onChange: setAgentPage, showTotal: (total: number) => `共 ${total} 个 Agent` }
  const skillPagination = { current: skillCurrentPage, pageSize: skillPageSize, showSizeChanger: false, onChange: (page: number) => { setExpandedPackage(null); setSkillPage(page) }, showTotal: (total: number) => `共 ${total} 条` }
  const [destroyModalOpen, setDestroyModalOpen] = useState(false)
  const [destroyType, setDestroyType] = useState<'agent' | 'skill' | 'package'>('agent')
  const [destroyId, setDestroyId] = useState<number>(0)
  const [destroyName, setDestroyName] = useState('')
  const [destroyStep, setDestroyStep] = useState<1 | 2>(1)
  const [destroyUsername, setDestroyUsername] = useState('')
  const [destroyPassword, setDestroyPassword] = useState('')
  const [destroyLoading, setDestroyLoading] = useState(false)
  // 技能包上传作业是否在跑：跑着就禁用"刷新技能缓存"（并发同步会互相覆盖，后端也会 409）
  const [uploadRunning, setUploadRunning] = useState(false)
  const prevUploadRunning = useRef(false)
  // 可选模型列表（编辑 Agent 时修改模型用；来源：后端 AVAILABLE_MODELS 配置）
  const { models: modelOptions, modelLabel } = useModelDirectory()
  const [gatewayModels, setGatewayModels] = useState<string[]>([])
  const [modelPolicies, setModelPolicies] = useState<ModelPolicy[]>([])
  const [policyUsers, setPolicyUsers] = useState<{ id: number; username: string }[]>([])
  const [policyScope, setPolicyScope] = useState<'role' | 'user'>('role')
  const [policyPrincipal, setPolicyPrincipal] = useState('viewer')
  const [policyEnabled, setPolicyEnabled] = useState(false)
  const [policySelection, setPolicySelection] = useState<string[]>([])
  const [policySaving, setPolicySaving] = useState(false)
  // 可绑定的 MCP 服务与知识库（新建/编辑 Agent 弹窗多选用）
  const [mcpServers, setMcpServers] = useState<McpServerInfo[]>([])
  const [knowledgeBases, setKnowledgeBases] = useState<KnowledgeBaseInfo[]>([])

  // 按权限拆分加载：只请求自己有权限的模块，避免单一权限时另一边 403 报错
  useEffect(() => {
    if (user?.role === 'admin' && (view === 'agents' || view === 'model-permissions')) {
      getModelPolicies().then((data) => {
        setGatewayModels(data.models || [])
        setModelPolicies(data.policies || [])
      }).catch(() => message.error('网关模型目录加载失败'))
      getAllUsers().then((data) => setPolicyUsers(data)).catch(() => {})
    }
    if (view === 'agents' && canManageAgent) {
      agentVersioning().then(setVersioning).catch(() => {})
      fetchAgents()
      listMcpServers(true).then(setMcpServers).catch(() => {})
      // 个人记忆库自动挂载到所有 Agent，不出现在可绑定选项里
      listKnowledgeBases()
        .then((list) => setKnowledgeBases(list.filter((k) => !k.name.startsWith(MEMORY_KB_PREFIX))))
        .catch(() => {})
    }
    if (view === 'skills' && canManageSkill) {
      skillVersioning().then(setVersioning).catch(() => {})
      fetchSkills()
    }
  }, [view, user?.id, user?.role, canManageAgent, canManageSkill])

  useEffect(() => {
    const policy = modelPolicies.find((entry) => entry.scope === policyScope && entry.principal === policyPrincipal)
    setPolicyEnabled(Boolean(policy))
    setPolicySelection(policy?.allowed_models || gatewayModels)
  }, [modelPolicies, policyScope, policyPrincipal, gatewayModels])

  const saveModelPolicy = async () => {
    if (!policyPrincipal) return message.warning('请选择用户或角色')
    if (policyEnabled && !policySelection.length) return message.warning('至少保留一个聊天模型')
    setPolicySaving(true)
    try {
      await setModelPolicy({
        scope: policyScope, principal: policyPrincipal,
        allowed_models: policyEnabled ? policySelection : null,
      })
      const data = await getModelPolicies()
      setModelPolicies(data.policies || [])
      message.success('模型权限已更新')
    } catch (error: any) {
      message.error(error.response?.data?.detail || '模型权限保存失败')
    } finally {
      setPolicySaving(false)
    }
  }

  const fetchAgents = async () => {
    const sequence = ++requestSequence.current
    setLoading(true); setLoadError('')
    try { const result = await getAllAgents(); if (sequence === requestSequence.current) setAgents(result) }
    catch (cause) { if (sequence === requestSequence.current) setLoadError(readableError(cause)) }
    finally { if (sequence === requestSequence.current) setLoading(false) }
  }
  const fetchSkills = async () => {
    const sequence = ++requestSequence.current
    setLoading(true); setLoadError('')
    try {
      const [skillList, pkgList] = await Promise.all([getAllSkillsAdmin(), getAllPackagesAdmin()])
      if (sequence !== requestSequence.current) return
      setSkills(skillList); setPackages(pkgList); void checkUploadJobs()
    } catch (cause) { if (sequence === requestSequence.current) setLoadError(readableError(cause)) }
    finally { if (sequence === requestSequence.current) setLoading(false) }
  }
  const handleCreateAgent = async (values: any) => {
    try {
      const payload = { name: values.name, description: values.description, agent_type: values.agent_type, system_prompt: values.system_prompt, config: {} }
      if (versioning) {
        const draft = await createAgentDraft(payload)
        setAgentModalOpen(false); agentForm.resetFields(); navigate(`/admin/agents/${draft.agent_id}`)
      } else {
        await createAgent(payload); message.success('助手创建成功'); setAgentModalOpen(false); agentForm.resetFields(); void fetchAgents()
      }
    } catch (cause) { message.error(readableError(cause)) }
  }
  const openEditAgent = (agent: Agent) => { setEditingAgent(agent); editAgentForm.setFieldsValue({ name: agent.name, description: agent.description, system_prompt: agent.system_prompt, model_config_name: agent.model_config_name, mcp_servers: agent.config?.mcp_servers || [], knowledge_bases: agent.config?.knowledge_bases || [], max_react_iters: agent.config?.max_react_iters ?? null }); setEditAgentModalOpen(true) }
  const handleUpdateAgent = async (values: any) => { try { if (editingAgent) { const { mcp_servers, knowledge_bases, max_react_iters, ...rest } = values; await updateAgent(editingAgent.id, { ...rest, config: { mcp_servers: mcp_servers || [], knowledge_bases: knowledge_bases || [], max_react_iters: max_react_iters ?? null } }); message.success('Agent更新成功') } setEditAgentModalOpen(false); setEditingAgent(null); editAgentForm.resetFields(); fetchAgents() } catch (e: any) { message.error(e.response?.data?.detail || '更新失败') } }
  const openBindingAgent = (agent: Agent) => { setBindingAgent(agent); bindingForm.setFieldsValue({ mcp_servers: agent.config?.mcp_servers || [], knowledge_bases: agent.config?.knowledge_bases || [] }); setBindingModalOpen(true) }
  const handleBindingSave = async (values: any) => { try { if (bindingAgent) { await updateAgent(bindingAgent.id, { config: { mcp_servers: values.mcp_servers || [], knowledge_bases: values.knowledge_bases || [] } }); message.success('绑定已保存') } setBindingModalOpen(false); setBindingAgent(null); bindingForm.resetFields(); fetchAgents() } catch (e: any) { message.error(e.response?.data?.detail || '保存失败') } }
  const handleDeleteAgent = async (id: number) => { try { await deleteAgent(id); message.success('Agent已删除'); fetchAgents() } catch { message.error('删除失败') } }
  const handleRestoreAgent = async (id: number) => { try { await restoreAgent(id); message.success('Agent已恢复'); fetchAgents() } catch { message.error('恢复失败') } }
  const handleCreateSkill = async (values: any) => { try { if (editingSkill) { await updateSkill(editingSkill.id, { ...values, allow_fork: user?.role === 'admin' && !editingSkill.package_id ? values.allow_fork : undefined }); message.success('Skill更新成功') } else { await createSkill(values); message.success('Skill创建成功') } setSkillModalOpen(false); setEditingSkill(null); skillForm.resetFields(); fetchSkills() } catch (e: any) { message.error(e.response?.data?.detail || '操作失败') } }
  // 删除/恢复失败时展示后端原因：目录缺失(409)、并发占用(409)、同步中止(503) 都需要原文
  const handleDeleteSkill = async (id: number) => { try { await deleteSkill(id); message.success('Skill已删除'); fetchSkills() } catch (e: any) { message.error(e.response?.data?.detail || '删除失败') } }
  const handleRestoreSkill = async (id: number) => { try { await restoreSkill(id); message.success('Skill已恢复'); fetchSkills() } catch (e: any) { message.error(e.response?.data?.detail || '恢复失败') } }
  const openEditSkill = (skill: Skill) => { setEditingSkill(skill); skillForm.setFieldsValue(skill); setSkillModalOpen(true) }

  // ===== 归属切换（可选 <-> 工具助手内置 <-> 数据分析师内置）=====
  // 后端是"剪切"语义：移动技能目录 + 重扫注册 + 锁定归属，成功后必须重拉列表
  const scopeOf = (s: Skill): SkillScope => (s.builtin_dialog === 1 ? 'dialog' : s.builtin_task === 1 ? 'task' : 'optional')
  const openScope = (skill: SkillTableRow) => { setScopeTarget(skill); setScopeValue(scopeOf(skill.__pkg ? skill.members[0] || {} as Skill : skill as Skill)) }
  const handleScopeSave = async () => {
    if (!scopeTarget) return
    setScopeSaving(true)
    try {
      if (scopeTarget.__pkg) await changePackageScope(scopeTarget.id, scopeValue)
      else await changeSkillScope(scopeTarget.id, scopeValue)
      message.success(`已切换到「${SCOPE_LABELS[scopeValue]}」`)
      setScopeTarget(null)
      fetchSkills()
    } catch (e: any) {
      // 后端拒绝原因（重名 / 目录缺失 / 整包移动 / 并发占用）都要原文展示
      message.error(e.response?.data?.detail || '归属切换失败')
    } finally {
      setScopeSaving(false)
    }
  }
  // 技能行（含技能包成员）的归属入口：三个分组的操作列共用
  const renderScopeButton = (r: Skill) => {
    if (!canSkillUpdate) return null
    const current = scopeOf(r)
    return (
      <Tooltip title={current === 'optional' ? '内置（切到工具助手/数据分析师）' : '取消内置 / 改内置归属'}>
        <Button
          type="text"
          size="small"
          icon={<PushpinOutlined style={current === 'optional' ? undefined : { color: 'var(--color-primary)' }} />}
          onClick={() => openScope(r)}
        />
      </Tooltip>
    )
  }
  // 技能包行：整包移动会拆散包结构（内置根不识别"包"），只给禁用入口 + 说明
  const renderPackageScopeButton = (pkg: SkillTableRow) => (
    <Tooltip title="修改整包内置归属（保留包结构）"><Button type="text" size="small" aria-label="修改技能包内置归属"
      icon={<PushpinOutlined />} onClick={() => openScope(pkg)} /></Tooltip>
  )
  // 技能包操作
  const openEditPkg = (pkg: SkillPackageInfo) => { setEditingPkg(pkg); pkgForm.setFieldsValue({ ...pkg, required_role: pkg.required_role ?? '' }); setPkgModalOpen(true) }
  const handleSavePkg = async (values: any) => { try { if (editingPkg) { await updatePackage(editingPkg.id, { ...values, allow_fork: user?.role === 'admin' ? values.allow_fork : undefined }); message.success('技能包更新成功') } setPkgModalOpen(false); setEditingPkg(null); pkgForm.resetFields(); fetchSkills() } catch (e: any) { message.error(e.response?.data?.detail || '操作失败') } }
  const handleDeletePackage = async (id: number) => { try { await deletePackage(id); message.success('技能包已删除（成员技能保留，用户端隐藏）'); fetchSkills() } catch (e: any) { message.error(e.response?.data?.detail || '删除失败') } }
  const handleRestorePackage = async (id: number) => { try { await restorePackage(id); message.success('技能包已恢复'); fetchSkills() } catch (e: any) { message.error(e.response?.data?.detail || '恢复失败') } }
  const openDestroyModal = (type: 'agent' | 'skill' | 'package', id: number, name: string) => { setDestroyType(type); setDestroyId(id); setDestroyName(name); setDestroyStep(1); setDestroyUsername(''); setDestroyPassword(''); setDestroyModalOpen(true) }
  const handleDestroyConfirm = async () => {
    setDestroyLoading(true)
    try {
      if (destroyType === 'agent') { await hardDeleteAgent(destroyId, destroyUsername, destroyPassword); message.success('Agent已彻底删除'); fetchAgents() }
      else if (destroyType === 'skill') { await hardDeleteSkill(destroyId, destroyUsername, destroyPassword); message.success('Skill已彻底删除'); fetchSkills() }
      else { await hardDeletePackage(destroyId, destroyUsername, destroyPassword); message.success('技能包及其全部技能已彻底删除'); fetchSkills() }
      setDestroyModalOpen(false)
    } catch (cause: any) {
      const detail = cause.response?.data?.detail
      if (detail?.code === 'skill_agent_bindings' && Array.isArray(detail.agents)) {
        Modal.error({ title: '以下助手仍内置绑定此技能', content: <div><p>{detail.message}</p><ul>{detail.agents.map((agent: { id: number; name: string; references: string[] }) => <li key={agent.id}><a href={`/admin/agents/${agent.id}`} target="_blank" rel="noopener noreferrer">{agent.name}（#{agent.id}）</a><p>{agent.references.join('；')}</p></li>)}</ul></div> })
      } else { message.error(readableError(cause)) }
    } finally { setDestroyLoading(false) }
  }
  const handleRefreshSkills = async () => { try { const res = await refreshSkills(); message.success(res?.message || '技能同步完成'); fetchSkills() } catch (e: any) { message.error(e.response?.data?.detail || '刷新失败') } }

  // 上传作业状态：进入页面/每次操作后随 fetchSkills 拉一次，作业在跑时再每 5 秒轮询
  const checkUploadJobs = async () => {
    try {
      setUploadRunning((await listUploadJobs(5)).some(isUploadJobRunning))
    } catch { /* 拿不到作业状态就不禁用按钮，后端仍有 409 兜底 */ }
  }
  useEffect(() => {
    if (!uploadRunning) return
    const t = window.setInterval(checkUploadJobs, 5000)
    return () => window.clearInterval(t)
  }, [uploadRunning])
  // 作业结束（可能是在技能广场页上传的）→ 自动刷新列表，管理员立刻看到新包
  useEffect(() => {
    if (prevUploadRunning.current && !uploadRunning) fetchSkills()
    prevUploadRunning.current = uploadRunning
  }, [uploadRunning])

  const activeAgents = agents.filter(a => a.is_active).length
  const activeSkills = skills.filter(s => s.is_active === 1).length

  const agentColumns = [
    { title: '序号', key: 'seq', width: '6%', align: 'center' as const, render: (_: any, _r: Agent, index: number) => (agentCurrentPage - 1) * 8 + index + 1 },
    // 名称列不设 width：吃掉剩余宽度，窗口变宽时优先给名称
    { title: '名称', dataIndex: 'name', key: 'name', width: '22%', ellipsis: true, render: (t: string, r: Agent) => <span style={{ fontWeight: 500 }}>{t}</span> },
    { title: '类型', dataIndex: 'agent_type', key: 'agent_type', width: '16%', align: 'center' as const, render: (t: string) => <Tag>{({ dialog: '工具助手', task: '数据分析师', rag: '知识助手' } as Record<string, string>)[t] || t}</Tag> },
    { title: '模型', dataIndex: 'model_config_name', key: 'model', width: '28%', align: 'center' as const, render: (modelId: string) => <Tag color="blue">{modelId ? modelLabel(modelId) : '未配置'}</Tag> },
    { title: '状态', key: 'is_active', width: '9%', align: 'center' as const, render: (_: any, r: Agent) => r.is_active ? <Tag color="success">活跃</Tag> : <Tag>已删除</Tag> },
    { title: '操作', key: 'action', width: '18%', align: 'center' as const, render: (_: any, r: Agent) => (
      <Space size={4}>
        {user?.role === 'admin' && versioning && <Button size="small" onClick={() => navigate(`/admin/agents/${r.id}`)}>配置</Button>}
        {!versioning && !BASE_AGENT_NAMES.includes(r.name) && canAgentUpdate && (
          <Tooltip title="编辑"><Button type="text" size="small" icon={<EditOutlined />} onClick={() => openEditAgent(r)} /></Tooltip>
        )}
        {!versioning && BASE_AGENT_NAMES.includes(r.name) && canAgentUpdate && (
          <Tooltip title="绑定知识库/MCP"><Button type="text" size="small" icon={<LinkOutlined />} onClick={() => openBindingAgent(r)} /></Tooltip>
        )}
        {r.is_active ? (
          canAgentDelete && <Popconfirm title="删除此 Agent？" description="删除后不可继续使用，保留历史记录并可恢复。" okText="删除" cancelText="取消" onConfirm={() => handleDeleteAgent(r.id)}><Button type="text" size="small" danger aria-label="删除" title="删除" icon={<DeleteOutlined />} /></Popconfirm>
        ) : (
          canAgentRestore && <Button type="text" size="small" icon={<UndoOutlined />} onClick={() => handleRestoreAgent(r.id)}>恢复</Button>
        )}
        {canAgentHard && <Button type="text" size="small" danger aria-label="彻底删除" title="彻底删除" icon={<StopOutlined />} onClick={() => openDestroyModal('agent', r.id, r.name)} />}
      </Space>
    ) }
  ]

  const skillColumns = [
    { title: '序号', key: 'seq', width: '6%', align: 'center' as const, render: (_: any, _r: Skill, index: number) => (skillCurrentPage - 1) * 8 + index + 1 },
    { title: '标识', dataIndex: 'name', key: 'name', width: '22%', align: 'center' as const, ellipsis: true },
    // 显示名称不设 width：吃掉剩余宽度
    { title: '显示名称', dataIndex: 'display_name', key: 'display_name', width: '22%', ellipsis: true, render: (t: string, r: Skill) => <span style={{ fontWeight: 500 }}>{r.display_name_zh || t}</span> },
    { title: '权限', dataIndex: 'required_role', key: 'required_role', width: '9%', align: 'center' as const, render: (t: string) => <Tag color={ROLE_COLORS[t]}>{ROLE_LABELS[t]}</Tag> },
    { title: '状态', key: 'is_active', width: '11%', align: 'center' as const, render: (_: any, r: Skill) => r.is_active === 1 ? <Space size={4}>{r.is_hidden && <Tooltip title="已对未授权的非管理员隐藏；被授权用户与管理员仍可见可用"><Tag color="blue" style={{ margin: 0 }}>广场隐藏</Tag></Tooltip>}<Tag color="success" style={{ margin: 0 }}>活跃</Tag></Space> : (r.dir_missing ? <Tooltip title="磁盘上找不到该技能目录，恢复会被拒绝；请重新上传技能包"><Tag color="warning">目录缺失</Tag></Tooltip> : <Tag>已删除</Tag>) },
    { title: '添加时间', dataIndex: 'created_at', key: 'created_at', width: '16%', align: 'center' as const, render: (t: string) => t ? t.replace('T', ' ').slice(0, 19) : '-' },
    { title: '操作', key: 'action', width: '18%', align: 'center' as const, render: (_: any, r: Skill) => (
      <Space size={4}>
        {canSkillUpdate && <Tooltip title="编辑"><Button type="text" size="small" aria-label="编辑技能" icon={<EditOutlined />} onClick={() => openEditSkill(r)} /></Tooltip>}
        {renderScopeButton(r)}
        {r.is_active === 1 ? (
          canSkillDelete && <Popconfirm title="删除此技能？" description="立即从技能广场及可选技能中移除，保留文件并可恢复。" okText="删除" cancelText="取消" onConfirm={() => handleDeleteSkill(r.id)}><Button type="text" size="small" danger aria-label="删除" title="删除" icon={<DeleteOutlined />} /></Popconfirm>
        ) : (
          canSkillRestore && <Button type="text" size="small" icon={<UndoOutlined />} onClick={() => handleRestoreSkill(r.id)}>恢复</Button>
        )}
        {canSkillHard && <Button type="text" size="small" danger aria-label="彻底删除" title="彻底删除" icon={<StopOutlined />} onClick={() => openDestroyModal('skill', r.id, r.display_name)} />}
      </Space>
    ) }
  ]

  // 可选组混合列：技能包行与普通技能行共用表格，按 __pkg 分支渲染
  const mixedColumns = [
    { title: '序号', key: 'seq', width: '6%', align: 'center' as const, render: (_: any, _r: SkillTableRow, index: number) => (skillCurrentPage - 1) * skillPageSize + index + 1 },
    { title: '标识', dataIndex: 'name', key: 'name', width: '22%', align: 'center' as const, ellipsis: true, render: (t: string, r: SkillTableRow) => r.__pkg ? <Space size={4}><span>{t}</span><Tag color="purple" style={{ margin: 0 }}>技能包</Tag></Space> : t },
    // 显示名称不设 width：吃掉剩余宽度
    { title: '显示名称', dataIndex: 'display_name', key: 'display_name', width: '22%', ellipsis: true, render: (t: string, r: SkillTableRow) => r.__pkg ? <Tooltip title={r.description || ''}><span style={{ fontWeight: 500 }}>{t}</span></Tooltip> : <span style={{ fontWeight: 500 }}>{(r as Skill).display_name_zh || t}</span> },
    { title: '权限', key: 'role', width: '9%', align: 'center' as const, render: (_: any, r: SkillTableRow) => r.__pkg ? (r.required_role ? <Tag color={ROLE_COLORS[r.required_role]}>{ROLE_LABELS[r.required_role]}</Tag> : '-') : <Tag color={ROLE_COLORS[(r as Skill).required_role]}>{ROLE_LABELS[(r as Skill).required_role]}</Tag> },
    { title: '状态', key: 'is_active', width: '11%', align: 'center' as const, render: (_: any, r: SkillTableRow) => r.is_active === 1 ? <Space size={4}>{r.is_hidden && <Tooltip title={r.__pkg ? '整包已对未授权的非管理员隐藏；被授权用户与管理员仍可见可用' : '已对未授权的非管理员隐藏；被授权用户与管理员仍可见可用'}><Tag color="blue" style={{ margin: 0 }}>广场隐藏</Tag></Tooltip>}<Tag color="success" style={{ margin: 0 }}>活跃</Tag></Space> : (r.dir_missing ? <Tooltip title="磁盘上找不到该目录，恢复会被拒绝；请重新上传技能包"><Tag color="warning">目录缺失</Tag></Tooltip> : <Tag>已删除</Tag>) },
    { title: '添加时间', dataIndex: 'created_at', key: 'created_at', width: '16%', align: 'center' as const, render: (t: string) => t ? t.replace('T', ' ').slice(0, 19) : '-' },
    { title: '操作', key: 'action', width: '18%', align: 'center' as const, render: (_: any, r: SkillTableRow) => r.__pkg ? (
      <Space size={4}>
        {canPkgUpdate && <Tooltip title="编辑"><Button type="text" size="small" aria-label="编辑技能包" icon={<EditOutlined />} onClick={() => openEditPkg(r)} /></Tooltip>}
        {canPkgUpdate && renderPackageScopeButton(r)}
        {r.is_active === 1 ? (
          canPkgDelete && <Popconfirm title="删除此技能包？" description="包内技能整体从技能广场及可选技能中移除，保留文件并可恢复。" okText="删除" cancelText="取消" onConfirm={() => handleDeletePackage(r.id)}><Button type="text" size="small" danger aria-label="删除" title="删除" icon={<DeleteOutlined />} /></Popconfirm>
        ) : (
          canPkgRestore && <Button type="text" size="small" icon={<UndoOutlined />} onClick={() => handleRestorePackage(r.id)}>恢复</Button>
        )}
        {canPkgHard && <Button type="text" size="small" danger aria-label="彻底删除" title="彻底删除" icon={<StopOutlined />} onClick={() => openDestroyModal('package', r.id, r.display_name)} />}
      </Space>
    ) : (
      <Space size="small">
        {canSkillUpdate && <Tooltip title="编辑"><Button type="text" size="small" aria-label="编辑技能" icon={<EditOutlined />} onClick={() => openEditSkill(r as Skill)} /></Tooltip>}
        {renderScopeButton(r as Skill)}
        {r.is_active === 1 ? (
          canSkillDelete && <Popconfirm title="删除此技能？" description="立即从技能广场及可选技能中移除，保留文件并可恢复。" okText="删除" cancelText="取消" onConfirm={() => handleDeleteSkill(r.id)}><Button type="text" size="small" danger aria-label="删除" title="删除" icon={<DeleteOutlined />} /></Popconfirm>
        ) : (
          canSkillRestore && <Button type="text" size="small" icon={<UndoOutlined />} onClick={() => handleRestoreSkill(r.id)}>恢复</Button>
        )}
        {canSkillHard && <Button type="text" size="small" danger aria-label="彻底删除" title="彻底删除" icon={<StopOutlined />} onClick={() => openDestroyModal('skill', r.id, (r as Skill).display_name)} />}
      </Space>
    ) }
  ]

  return (
    <div className={`admin-layout admin-resource-page admin-resource-${view}${view === 'skills' && canPkgUpload ? ' admin-skill-has-upload' : ''}`}>
      {view === 'skills' && canPkgUpload && <div className="admin-skill-upload-slot"><SkillPackageUpload onDone={() => void fetchSkills()} /></div>}
      <header className="admin-header">
        <div className="admin-header-left">
          <PlatformLogo size={32} />
          <span style={{ fontSize: 18, fontWeight: 600, color: 'var(--text-primary)' }}>
            {view === 'agents' ? 'Agent 管理' : view === 'skills' ? 'Skill 管理' : '模型授权'}
          </span>
        </div>
        <div className="admin-header-right">
          <Tooltip title={theme === 'light' ? '切换到暗色模式' : '切换到亮色模式'}>
            <Button type="text" icon={<BulbOutlined />} onClick={toggleTheme} style={{ borderRadius: 8 }} />
          </Tooltip>
          <Button icon={<ArrowLeftOutlined />} onClick={() => navigate('/admin')} style={{ borderRadius: 8 }}>返回管理后台</Button>
        </div>
      </header>

      <div className="admin-content">
        {loadError && <Alert type="error" message={loadError} action={<Button size="small" onClick={() => void (view === 'agents' ? fetchAgents() : fetchSkills())}>重试</Button>} />}
        <div className="admin-stats">
          {view === 'agents' && canManageAgent && (
          <div className="admin-stat-card stagger-item">
            <div className="admin-stat-icon" style={{ background: 'var(--color-primary-bg)', color: 'var(--color-primary)' }}><RobotOutlined /></div>
            <div><div className="admin-stat-value">{activeAgents}</div><div className="admin-stat-label">活跃 Agent</div></div>
          </div>
          )}
          {view === 'skills' && canManageSkill && (
          <div className="admin-stat-card stagger-item">
            <div className="admin-stat-icon" style={{ background: 'rgba(22, 163, 74, 0.12)', color: '#16A34A' }}><ToolOutlined /></div>
            <div><div className="admin-stat-value">{activeSkills}</div><div className="admin-stat-label">活跃技能</div></div>
          </div>
          )}
          {view === 'agents' && canManageAgent && (
          <div className="admin-stat-card stagger-item">
            <div className="admin-stat-icon" style={{ background: 'rgba(217, 119, 6, 0.12)', color: '#D97706' }}><DashboardOutlined /></div>
            <div><div className="admin-stat-value">{agents.length}</div><div className="admin-stat-label">Agent 总数</div></div>
          </div>
          )}
        </div>

        {view === 'model-permissions' && user?.role === 'admin' && (
          <div className="admin-section">
            <div className="admin-section-header">
              <div className="admin-section-title"><RobotOutlined /> 模型权限</div>
              <Button type="primary" onClick={saveModelPolicy} loading={policySaving} disabled={!gatewayModels.length}>保存配置</Button>
            </div>
            <p style={{ color: 'var(--text-secondary)' }}>仅配置网关中的聊天模型；个人配置覆盖角色配置，不影响其他用户。未单独配置时继承角色，角色未配置时开放所有聊天模型。</p>
            <Space wrap style={{ width: '100%', marginBottom: 12 }}>
              <Select value={policyScope} style={{ width: 120 }} options={[{ value: 'role', label: '角色' }, { value: 'user', label: '单个用户' }]} onChange={(value) => { setPolicyScope(value); setPolicyPrincipal(value === 'role' ? 'viewer' : '') }} />
              <Select value={policyPrincipal || undefined} style={{ width: 220 }} showSearch optionFilterProp="label" placeholder="选择对象" options={policyScope === 'role'
                ? [{ value: 'viewer', label: '观察者' }, { value: 'operator', label: '操作员' }, { value: 'admin', label: '管理员' }]
                : policyUsers.map((account) => ({ value: String(account.id), label: account.username }))}
                onChange={setPolicyPrincipal} />
              <Switch checked={policyEnabled} onChange={setPolicyEnabled} disabled={!policyPrincipal} />
              <span style={{ color: 'var(--text-secondary)' }}>{policyEnabled ? '单独指定可用模型' : policyScope === 'user' ? '继承角色' : '使用网关全部聊天模型'}</span>
            </Space>
            {policyEnabled && <Select mode="multiple" style={{ width: '100%' }} allowClear showSearch optionFilterProp="label" placeholder="选择允许的聊天模型" value={policySelection} options={gatewayModels.map((modelId) => ({ value: modelId, label: modelLabel(modelId) }))} onChange={setPolicySelection} />}
          </div>
        )}

        {view === 'agents' && canManageAgent && (
        <div className="admin-section">
          <div className="admin-section-header">
            <span className="admin-resource-count">{loading ? '正在加载…' : `共 ${filteredAgents.length} 个助手`}</span>
            <Space>
              <Input placeholder="搜索 Agent" prefix={<SearchOutlined />} allowClear value={agentSearch} onChange={(event) => { setAgentSearch(event.target.value); setAgentPage(1) }} style={{ width: 200 }} />
              {canAgentCreate && <Button type="primary" icon={<PlusOutlined />} onClick={() => { agentForm.resetFields(); setAgentModalOpen(true) }} style={{ borderRadius: 8 }}>新建 Agent</Button>}
            </Space>
          </div>
          <div className="admin-table">
            <Table columns={agentColumns} dataSource={filteredAgents} rowKey="id" loading={loading} pagination={agentPagination} tableLayout="fixed" scroll={{ x: TABLE_MIN_WIDTH }} rowClassName={(r: Agent) => !r.is_active ? 'row-disabled' : ''} />
          </div>
        </div>
        )}

        {view === 'skills' && canManageSkill && (
        <div className="admin-section">
          <div className="admin-section-header">
            <span className="admin-resource-count">{loading ? '正在加载…' : `共 ${skillTotal} 项技能 / 技能包`}</span>
            <Space className="admin-skill-search-actions">
              <Input placeholder="搜索技能或技能包" prefix={<SearchOutlined />} allowClear value={skillSearch} onChange={(event) => { setSkillSearch(event.target.value); setExpandedPackage(null); setSkillPage(1) }} style={{ width: 200 }} />
              {canSkillRefresh && (
                <Tooltip title={uploadRunning ? '上传作业进行中，暂不可刷新' : '刷新技能缓存'}>
                  <Button icon={<ReloadOutlined />} onClick={handleRefreshSkills} disabled={uploadRunning} loading={uploadRunning} style={{ borderRadius: 8 }} />
                </Tooltip>
              )}
              {canSkillCreate && <Button type="primary" icon={<PlusOutlined />} onClick={() => { skillForm.resetFields(); setEditingSkill(null); setSkillModalOpen(true) }} style={{ borderRadius: 8 }}>新建 Skill</Button>}
            </Space>
          </div>
          <div className="admin-table">
            <div style={{ marginBottom: 12 }}>
              <Segmented
                value={skillGroup}
                onChange={(v) => { setSkillGroup(v as 'optional' | 'dialog' | 'task'); setExpandedPackage(null); setSkillPage(1) }}
                options={[
                  { label: '可选skill', value: 'optional' },
                  { label: '工具助手skill', value: 'dialog' },
                  { label: '数据分析师skill', value: 'task' },
                ]}
              />
            </div>
            <Table columns={mixedColumns} dataSource={optionalDataSource}
              rowKey={(row: SkillTableRow) => row.__pkg ? `p${row.id}` : `s${row.id}`}
              loading={loading} pagination={skillPagination} tableLayout="fixed" scroll={{ x: TABLE_MIN_WIDTH }}
              rowClassName={(row: SkillTableRow) => row.is_active !== 1 ? 'row-disabled' : row.__pkg ? 'skill-package-row' : ''}
              expandable={{ childrenColumnName: '__unused', expandedRowKeys: expandedPackage ? [`p${expandedPackage.id}`] : [], rowExpandable: (row) => !!row.__pkg,
                expandIconColumnIndex: 1, onExpand: (expanded, row) => {
                  if (row.__pkg && expanded) { setExpandedPackage(row); setSkillPage(Math.floor(optionalDataSource.indexOf(row) / 3) + 1) }
                  else { setExpandedPackage(null); setSkillPage(Math.floor(optionalDataSource.indexOf(row) / 8) + 1) }
                }, expandedRowRender: (row) => row.__pkg ? <div className="skill-package-children">
                  <div className="skill-package-caption">{row.display_name} / 包内技能</div>
                  <Table dataSource={row.members} rowKey="id" size="small" tableLayout="fixed" pagination={{ pageSize: 4, showSizeChanger: false }}
                    columns={skillColumns.map((column) => column.key === 'seq' ? { ...column, render: (_: any, skill: Skill) => `${optionalDataSource.indexOf(row) + 1}.${row.members.indexOf(skill) + 1}` } : column)} />
                </div> : null }} />

          </div>
        </div>
        )}
      </div>

      <Modal className="admin-modal" title={editingSkill ? '编辑 Skill' : '新建 Skill'} open={skillModalOpen} onCancel={() => { setSkillModalOpen(false); setEditingSkill(null) }} onOk={() => skillForm.submit()} okText="确定" cancelText="取消">
        {editingSkill && user?.role === 'admin' && <Space style={{ marginBottom: 16 }}>
          <Button onClick={() => navigate(`/admin/skills/${editingSkill.id}`)}>版本与文件</Button>
          <SkillResourceUpdate key={`skill-${editingSkill.id}`} kind="skill" id={editingSkill.id} onDone={() => void fetchSkills()} />
        </Space>}
        <Form form={skillForm} onFinish={handleCreateSkill} layout="vertical">
          <Form.Item name="name" label="标识 (name)" rules={[{ required: true, message: '请输入标识' }]}><Input placeholder="例如：calculator（创建后不可修改）" disabled={!!editingSkill} /></Form.Item>
          <Form.Item name="display_name" label="显示名称" rules={[{ required: true, message: '请输入显示名称' }]}><Input placeholder="例如：数学计算器" /></Form.Item>
          <Form.Item name="description" label="描述"><TextArea placeholder="Skill的功能描述" rows={2} /></Form.Item>
          <Form.Item name="required_role" label="所需权限" initialValue="viewer">
            <Radio.Group
              optionType="button"
              buttonStyle="solid"
              options={[
                { label: '观察者', value: 'viewer' },
                { label: '操作员', value: 'operator' },
                { label: '管理员', value: 'admin' },
              ]}
            />
          </Form.Item>
          {editingSkill && user?.role === 'admin' && (editingSkill.package_id ? <Alert type="info" showIcon style={{ marginBottom: 16 }} message="包内技能继承整包派生许可，请在技能包的编辑中设置。" /> : <Form.Item name="allow_fork" label="允许派生与文件预览" valuePropName="checked" extra="仅在确认来源与许可后开放。关闭后广场仍显示目录结构，但不可打开文件；已派生的副本不回收。"><Switch /></Form.Item>)}
          <Form.Item name="is_hidden" label="在技能广场隐藏" valuePropName="checked" initialValue={false}>
            <Switch />
          </Form.Item>
        </Form>
      </Modal>

      <Modal className="admin-modal" title="编辑技能包" open={pkgModalOpen} onCancel={() => { setPkgModalOpen(false); setEditingPkg(null) }} onOk={() => pkgForm.submit()} okText="确定" cancelText="取消">
        {editingPkg && user?.role === 'admin' && <Space style={{ marginBottom: 16 }}>
          <Button onClick={() => navigate(`/admin/skill-packages/${editingPkg.id}`)}>版本与文件</Button>
          <SkillResourceUpdate key={`package-${editingPkg.id}`} kind="package" id={editingPkg.id} onDone={() => void fetchSkills()} />
        </Space>}
        <Form form={pkgForm} onFinish={handleSavePkg} layout="vertical">
          <Form.Item name="display_name" label="包短名" rules={[{ required: true, message: '请输入包短名' }]}><Input placeholder="例如：阿里工具包" /></Form.Item>
          <Form.Item name="description" label="包描述"><TextArea placeholder="技能包的能力与用途（约50字）" rows={3} /></Form.Item>
          <Form.Item name="required_role" label="所需权限（设置后包内所有技能统一沿用；不设置则各技能独立）">
            <Select getPopupContainer={(trigger) => (trigger.closest('.ant-modal-content') as HTMLElement) || document.body}>
              <Select.Option value="">不设置</Select.Option>
              <Select.Option value="viewer">观察者</Select.Option>
              <Select.Option value="operator">操作员</Select.Option>
              <Select.Option value="admin">管理员</Select.Option>
            </Select>
          </Form.Item>
          {user?.role === 'admin' && <Form.Item name="allow_fork" label="允许整包派生与文件预览" valuePropName="checked" extra="包内所有技能统一继承本许可，不能单独绕过。关闭后仍可逐层浏览目录，不能打开文件。"><Switch /></Form.Item>}
          <Form.Item name="is_hidden" label="在技能广场隐藏整包" valuePropName="checked" initialValue={false}>
            <Switch />
          </Form.Item>
        </Form>
      </Modal>

      {/* 归属切换：可选 / 工具助手内置 / 数据分析师内置 */}
      <Modal
        className="admin-modal"
        title={`技能归属 · ${scopeTarget?.display_name || scopeTarget?.name || ''}`}
        open={!!scopeTarget}
        onCancel={() => setScopeTarget(null)}
        onOk={handleScopeSave}
        confirmLoading={scopeSaving}
        okText="确定"
        cancelText="取消"
      >
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 16, borderRadius: 8 }}
          message={scopeTarget?.__pkg ? "整体设置技能包内置归属，保留包结构" : "切换技能内置归属"}
          description="内置后技能不再出现在可选技能与技能广场，由对应 Agent 原生装载；取消内置则回到可选技能。归属会锁定，刷新技能不会把它打回原形。"
        />
        <Segmented
          block
          value={scopeValue}
          onChange={(v) => setScopeValue(v as SkillScope)}
          options={[
            { label: SCOPE_LABELS.optional, value: 'optional' },
            { label: SCOPE_LABELS.dialog, value: 'dialog' },
            { label: SCOPE_LABELS.task, value: 'task' },
          ]}
        />
        {scopeTarget && !scopeTarget.__pkg && scopeTarget.package_id ? (
          <div className="admin-scope-hint">该技能当前属于技能包，切换后将脱离技能包成为独立技能，包内其余成员不受影响。</div>
        ) : null}
      </Modal>

      <Modal className="admin-modal" title="新建 Agent" open={agentModalOpen} onCancel={() => setAgentModalOpen(false)} onOk={() => agentForm.submit()} okText="创建" cancelText="取消">
        <Form form={agentForm} onFinish={handleCreateAgent} layout="vertical">
          <Form.Item name="name" label="名称" rules={[{ required: true, message: '请输入名称' }]}><Input placeholder="Agent名称" /></Form.Item>
          <Form.Item name="description" label="描述"><TextArea placeholder="Agent的功能描述" rows={2} /></Form.Item>
          <Form.Item name="agent_type" label="基底Agent" initialValue="dialog"><Select getPopupContainer={(trigger) => (trigger.closest('.ant-modal-content') as HTMLElement) || document.body}><Select.Option value="dialog">工具助手底座</Select.Option><Select.Option value="task">数据分析师底座</Select.Option></Select></Form.Item>
          <Form.Item name="system_prompt" label="系统提示词"><TextArea placeholder="Agent的系统提示词" rows={4} /></Form.Item>

        </Form>
      </Modal>

      <Modal className="admin-modal" title={`绑定知识库 / MCP · ${bindingAgent?.name || ''}`} open={bindingModalOpen} onCancel={() => { setBindingModalOpen(false); setBindingAgent(null) }} onOk={() => bindingForm.submit()} okText="保存" cancelText="取消">
        <Alert type="info" showIcon message="基底 Agent 的名称与提示词不可修改，这里只调整知识库 / MCP 绑定。用户的个人知识库会自动纳入其检索范围，无需在此绑定。" style={{ marginBottom: 12, borderRadius: 8 }} />
        <Form form={bindingForm} onFinish={handleBindingSave} layout="vertical">
          <Form.Item name="mcp_servers" label="绑定 MCP 服务" tooltip="Agent 可调用所选 MCP 服务的工具">
            <Select mode="multiple" allowClear placeholder="选择要绑定的 MCP 服务" options={mcpServers.filter((m) => m.enabled).map((m) => ({ value: m.id, label: `${m.name}（${m.type}）` }))} getPopupContainer={(trigger) => (trigger.closest('.ant-modal-content') as HTMLElement) || document.body} />
          </Form.Item>
          <Form.Item name="knowledge_bases" label="绑定知识库" tooltip="所有用户使用该 Agent 时可检索所选共享知识库">
            <Select mode="multiple" allowClear placeholder="选择要绑定的知识库" options={knowledgeBases.map((k) => ({ value: k.id, label: k.name }))} getPopupContainer={(trigger) => (trigger.closest('.ant-modal-content') as HTMLElement) || document.body} />
          </Form.Item>
        </Form>
      </Modal>

      <Modal className="admin-modal" title="编辑 Agent" open={editAgentModalOpen} onCancel={() => { setEditAgentModalOpen(false); setEditingAgent(null) }} onOk={() => editAgentForm.submit()} okText="保存" cancelText="取消">
        <Form form={editAgentForm} onFinish={handleUpdateAgent} layout="vertical">
          <Form.Item name="name" label="名称" rules={[{ required: true, message: '请输入名称' }]}><Input placeholder="Agent名称" /></Form.Item>
          <Form.Item name="description" label="描述"><TextArea placeholder="Agent的功能描述" rows={2} /></Form.Item>
          <Form.Item name="model_config_name" label="模型" tooltip="用户未手动选模型时使用该模型；聊天时用户仍可按会话临时切换">
            <Select options={modelOptions.map((modelId) => ({ value: modelId, label: modelLabel(modelId) }))} />
          </Form.Item>
          <Form.Item name="mcp_servers" label="绑定 MCP 服务" tooltip="Agent 可调用所选 MCP 服务的工具">
            <Select mode="multiple" allowClear placeholder="选择要绑定的 MCP 服务" options={mcpServers.filter((m) => m.enabled).map((m) => ({ value: m.id, label: `${m.name}（${m.type}）` }))} getPopupContainer={(trigger) => (trigger.closest('.ant-modal-content') as HTMLElement) || document.body} />
          </Form.Item>
          <Form.Item name="knowledge_bases" label="绑定知识库" tooltip="Agent 可检索所选知识库的内容">
            <Select mode="multiple" allowClear placeholder="选择要绑定的知识库" options={knowledgeBases.map((k) => ({ value: k.id, label: k.name }))} getPopupContainer={(trigger) => (trigger.closest('.ant-modal-content') as HTMLElement) || document.body} />
          </Form.Item>
          <Form.Item name="system_prompt" label="系统提示词"><TextArea placeholder="Agent的系统提示词" rows={4} /></Form.Item>
          <Form.Item name="max_react_iters" label="最大 ReAct 轮次" tooltip="留空则不设限"><InputNumber min={1} placeholder="留空=不设限" style={{ width: '100%' }} /></Form.Item>
        </Form>
      </Modal>

      <Modal className="admin-modal" title={<span style={{ color: '#ef4444' }}><ExclamationCircleOutlined style={{ marginRight: 8 }} />彻底删除 {destroyType === 'agent' ? 'Agent' : destroyType === 'skill' ? 'Skill' : '技能包'}</span>} open={destroyModalOpen} onCancel={() => { setDestroyModalOpen(false); setDestroyStep(1); setDestroyUsername(''); setDestroyPassword('') }} closable={false} footer={destroyStep === 1 ? (<Space><Button onClick={() => { setDestroyModalOpen(false); setDestroyStep(1) }}>取消</Button><Button type="primary" danger disabled={!destroyUsername || !destroyPassword} onClick={() => setDestroyStep(2)}>下一步</Button></Space>) : (<Space><Button onClick={() => setDestroyStep(1)}>返回</Button><Button type="primary" danger loading={destroyLoading} onClick={handleDestroyConfirm}>确认删除</Button></Space>)}>
        <Alert message="危险操作！此操作不可恢复！" description={destroyType === 'package' ? '彻底删除技能包将同时删除包内全部技能（数据库与磁盘文件），无法找回。' : '彻底删除后，该记录将从数据库中永久移除，无法找回。'} type="error" showIcon style={{ marginBottom: 16, borderRadius: 8 }} />
        {destroyStep === 1 ? (
          <div><p style={{ marginBottom: 12 }}>请输入管理员账号和密码以继续：</p><Input placeholder="管理员账号" value={destroyUsername} onChange={(e) => setDestroyUsername(e.target.value)} style={{ marginBottom: 8, borderRadius: 8 }} size="large" /><Input.Password placeholder="管理员密码" value={destroyPassword} onChange={(e) => setDestroyPassword(e.target.value)} size="large" style={{ borderRadius: 8 }} onPressEnter={() => { if (destroyUsername && destroyPassword) setDestroyStep(2) }} /></div>
        ) : (
          <div><Alert message="最终确认" type="warning" showIcon style={{ marginBottom: 16, borderRadius: 8 }} /><Descriptions bordered size="small" column={1}><Descriptions.Item label="操作">彻底删除（不可恢复）</Descriptions.Item><Descriptions.Item label="类型">{destroyType === 'agent' ? 'Agent' : destroyType === 'skill' ? 'Skill' : '技能包（含包内全部技能）'}</Descriptions.Item><Descriptions.Item label="名称"><strong style={{ color: '#ef4444' }}>{destroyName}</strong></Descriptions.Item><Descriptions.Item label="管理员账号">{destroyUsername}</Descriptions.Item></Descriptions><p style={{ marginTop: 12, color: '#ef4444', fontWeight: 600 }}>点击“确认删除”后，数据将永久丢失！</p></div>
        )}
      </Modal>

      <style>{`.row-disabled { opacity: 0.5; }`}</style>
    </div>
  )
}
