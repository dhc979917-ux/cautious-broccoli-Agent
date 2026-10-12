import { useCallback, useEffect, useMemo, useState } from 'react'
import axios from 'axios'
import {
  Alert, Button, Descriptions, Drawer, Empty, Form, Input, message, Modal,
  Select, Space, Spin, Table, Tabs, Tag, Tree,
} from 'antd'
import { ReloadOutlined, TeamOutlined } from '@ant-design/icons'
import { useAuthStore } from '../../store/authStore'
import { getAllAgents, getSkillGrantOptions, SkillGrantOptions } from '../../api/admin'
import {
  addDepartment, addRole, addRoleTemplate, Department, editDepartment, editPerson,
  editResources, editRoleTemplate, getDepartments, getOrgAccess, getPeople, getPerson,
  getRoleTemplates, PeopleFilters, PeoplePage, Person, removeRole, RoleTemplate, ScopeType,
} from '../../api/people'
import { buildPermTreeData, collapseToCodes, expandToCheckedKeys } from '../../constants/permissions'
import { ROLE_LABELS } from '../../constants/roles'
import AccountDialog, { type AccountAction } from './AccountDialog'
import './index.css'
import SkillGrantSelect from '../../components/SkillGrantSelect'
import ModelExtraGrants from '../../components/ModelExtraGrants'
import { permissionLabels, roleScopeLabel } from '../../utils/peopleLabels'

type AgentOption = { id: number; name: string; is_active: boolean }

function detail(error: unknown): string {
  if (axios.isAxiosError(error)) {
    if (error.response?.status === 409) return '数据已被其他人修改，请刷新后重试。'
    if (error.response?.status === 403) return '当前账号无权执行该操作。'
    if (error.response?.status === 503) return '审计或服务暂不可用，请稍后重试。'
    const responseDetail = error.response?.data?.detail
    if (typeof responseDetail === 'string') return responseDetail
  }
  return '请求失败，请稍后重试。'
}

function displayStatus(person: Person): string {
  if (person.employment_status === 'departed') return '已离职'
  return person.is_active ? '启用' : '停用'
}

export default function PeopleAdmin() {
  const user = useAuthStore((state) => state.user)
  const isAdmin = user?.role === 'admin'
  const [canView, setCanView] = useState<boolean | null>(null)
  const [departments, setDepartments] = useState<Department[]>([])
  const [templates, setTemplates] = useState<RoleTemplate[]>([])
  const [filters, setFilters] = useState<PeopleFilters>({ page: 1, page_size: 20, sort_by: 'last_login', sort_order: 'desc' })
  const [search, setSearch] = useState('')
  const [page, setPage] = useState<PeoplePage>({ items: [], total: 0, page: 1, page_size: 20 })
  const [refreshKey, setRefreshKey] = useState(0)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [selected, setSelected] = useState<Person | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [editor, setEditor] = useState<'person' | 'department' | 'template' | 'role' | null>(null)
  const [editingDepartment, setEditingDepartment] = useState<Department | null>(null)
  const [editingTemplate, setEditingTemplate] = useState<RoleTemplate | null>(null)
  const [permChecked, setPermChecked] = useState<string[]>([])
  const [agentOptions, setAgentOptions] = useState<AgentOption[]>([])
  const [skillOptions, setSkillOptions] = useState<SkillGrantOptions | null>(null)
  const [grantError, setGrantError] = useState<string | null>(null)
  const [personForm] = Form.useForm()
  const [departmentForm] = Form.useForm()
  const [templateForm] = Form.useForm()
  const [roleForm] = Form.useForm()
  const [resourceForm] = Form.useForm()
  const scopeType: ScopeType | undefined = Form.useWatch('scope_type', roleForm)
  const [accountAction, setAccountAction] = useState<AccountAction | null>(null)
  const [detailTab, setDetailTab] = useState('base')

  useEffect(() => {
    let alive = true
    Promise.all([getOrgAccess(), getDepartments(), getRoleTemplates()]).then(([access, dirs, roles]) => {
      if (!alive) return
      setCanView(access.can_view_people)
      setDepartments(dirs)
      setTemplates(roles)
    }).catch((reason) => { if (alive) { setCanView(false); setError(detail(reason)) } })
    return () => { alive = false }
  }, [])

  useEffect(() => {
    if (!canView) return
    let alive = true
    setLoading(true)
    setError(null)
    getPeople(filters).then((result) => { if (alive) setPage(result) }).catch((reason) => {
      if (alive) setError(detail(reason))
    }).finally(() => { if (alive) setLoading(false) })
    return () => { alive = false }
  }, [canView, filters, refreshKey])

  useEffect(() => {
    if (!selected || !isAdmin) return
    let alive = true
    setGrantError(null)
    Promise.all([getAllAgents(), getSkillGrantOptions()]).then(([agents, skills]) => {
      if (alive) { setAgentOptions(agents); setSkillOptions(skills) }
    }).catch((reason) => { if (alive) setGrantError(detail(reason)) })
    return () => { alive = false }
  }, [selected?.id, isAdmin])

  useEffect(() => {
    if (selected) resourceForm.setFieldsValue({
      agent_ids: selected.agent_ids,
      skills: selected.skill_grants,
    })
  }, [selected, resourceForm])

  const departmentOptions = useMemo(() => departments.map((department) => ({
    value: department.id,
    label: department.name,
    disabled: !department.is_active,
  })), [departments])

  const changeFilter = (change: Partial<PeopleFilters>) => {
    setFilters((current) => ({ ...current, ...change, page: 1 }))
  }

  const refresh = useCallback(() => { setRefreshKey((value) => value + 1) }, [])

  const openPerson = async (personId: number) => {
    setDetailTab('base')
    setDetailLoading(true)
    try {
      setSelected(await getPerson(personId))
    } catch (reason) {
      message.error(detail(reason))
    } finally {
      setDetailLoading(false)
    }
  }

  const afterPersonChange = async (personId: number) => {
    setSelected(await getPerson(personId))
    refresh()
  }

  const runPersonChange = async (operation: () => Promise<Person>) => {
    if (!selected) return
    setSaving(true)
    try {
      await operation()
      await afterPersonChange(selected.id)
      setEditor(null)
      message.success('修改已保存；相关授权变更已使旧会话失效。')
    } catch (reason) {
      message.error(detail(reason))
      if (axios.isAxiosError(reason) && reason.response?.status === 409) {
        await openPerson(selected.id)
      }
    } finally {
      setSaving(false)
    }
  }

  const showPersonEditor = () => {
    if (!selected) return
    personForm.setFieldsValue({
      department_id: selected.department_id, job_title: selected.job_title,
      external_id: selected.external_id,
      source: selected.source, employment_status: selected.employment_status,
      role: selected.role, is_active: selected.is_active,
    })
    setEditor('person')
  }

  const showDepartmentEditor = (department: Department | null) => {
    setEditingDepartment(department)
    departmentForm.setFieldsValue(department || { code: '', name: '', parent_id: null })
    setEditor('department')
  }

  const showTemplateEditor = (template: RoleTemplate | null) => {
    setEditingTemplate(template)
    templateForm.setFieldsValue(template || { code: '', name: '' })
    setPermChecked(template ? expandToCheckedKeys(template.permissions) : [])
    setEditor('template')
  }

  const saveDepartment = async () => {
    try {
      const values = await departmentForm.validateFields()
      setSaving(true)
      if (editingDepartment) {
        await editDepartment(editingDepartment.id, { revision: editingDepartment.revision,
          name: values.name, parent_id: values.parent_id ?? null })
      } else {
        await addDepartment(values)
      }
      setDepartments(await getDepartments())
      setEditor(null)
      message.success('部门已保存')
    } catch (reason) { if (!('errorFields' in (reason as object))) message.error(detail(reason)) }
    finally { setSaving(false) }
  }

  const saveTemplate = async () => {
    try {
      const values = await templateForm.validateFields()
      const permissions = collapseToCodes(permChecked)
      if (!permissions.length) { message.error('请选择至少一项权限'); return }
      setSaving(true)
      if (editingTemplate) {
        await editRoleTemplate(editingTemplate.id, { revision: editingTemplate.revision,
          name: values.name, permissions })
      } else {
        await addRoleTemplate({ ...values, permissions })
      }
      setTemplates(await getRoleTemplates())
      setEditor(null)
      message.success('角色模板已保存，相关会话会重新验证权限')
    } catch (reason) { if (!('errorFields' in (reason as object))) message.error(detail(reason)) }
    finally { setSaving(false) }
  }

  const changeTemplateActive = (template: RoleTemplate) => {
    Modal.confirm({ title: template.is_active ? '停用此角色模板？' : '启用此角色模板？',
      content: '角色变更后，相关账号旧会话将失效。',
      onOk: async () => {
        try {
          await editRoleTemplate(template.id, { revision: template.revision, is_active: !template.is_active })
          setTemplates(await getRoleTemplates())
        } catch (reason) { message.error(detail(reason)) }
      },
    })
  }

  const changeDepartmentActive = (department: Department) => {
    Modal.confirm({ title: department.is_active ? '停用此部门？' : '启用此部门？',
      content: '有成员或下级部门时无法停用。',
      onOk: async () => {
        try {
          await editDepartment(department.id, { revision: department.revision,
            is_active: !department.is_active })
          setDepartments(await getDepartments())
        } catch (reason) { message.error(detail(reason)) }
      },
    })
  }

  const grantRole = async () => {
    if (!selected) return
    try {
      const values = await roleForm.validateFields()
      const scope: number[] | { type: string; ids: number[] } | undefined =
        values.scope_type === 'departments' ? values.department_ids :
          values.scope_type === 'resource' ? {
            type: values.resource_type,
            ids: String(values.resource_ids || '').split(',').map((part) => Number(part.trim())),
          } : undefined
      await runPersonChange(() => addRole(selected.id, { revision: selected.revision,
        template_id: values.template_id, scope_type: values.scope_type, scope }))
    } catch (reason) { if (!('errorFields' in (reason as object))) message.error(detail(reason)) }
  }

  const revokeRole = (assignmentId: number) => {
    if (!selected) return
    Modal.confirm({ title: '撤销该角色授权？', content: '操作后人员的旧令牌立即失效。',
      onOk: () => runPersonChange(() => removeRole(selected.id, assignmentId, selected.revision)),
    })
  }

  const details = selected ? [
    { key: 'base', label: '基本信息', children: <Space direction="vertical" className="people-detail-content">
      <Descriptions size="small" bordered column={1} items={[
        { key: 'account', label: '账号', children: selected.account || '—' },
        { key: 'department', label: '部门', children: selected.department_name || '未分配' },
        { key: 'job', label: '职位', children: selected.job_title || '未填写' },
        { key: 'external', label: '外部标识', children: selected.external_id || '未接入' },
        { key: 'source', label: '来源', children: selected.source || '本地账号' },
        { key: 'email', label: '邮箱', children: selected.email || '未填写' },
        { key: 'state', label: '状态', children: displayStatus(selected) },
        { key: 'last-login', label: '最近登录', children: selected.last_login ? new Date(selected.last_login).toLocaleString() : '暂无记录' },
      ]} />
      {selected.can_edit && <Button type="primary" onClick={showPersonEditor}>编辑人员信息</Button>}
    </Space> },
    { key: 'roles', label: '角色权限', children: <Space direction="vertical" className="people-detail-content">
      <Tag color="blue">基础角色：{ROLE_LABELS[selected.role] || selected.role}</Tag>
      {selected.roles.length ? selected.roles.map((assignment) => <div className="people-grant" key={assignment.id}>
        <div><strong>{assignment.name}</strong><span>数据范围：{roleScopeLabel(assignment, departments)}</span></div>
        {isAdmin && user?.id !== selected.id && <Button size="small" danger onClick={() => revokeRole(assignment.id)}>撤销</Button>}
      </div>) : <Empty description="尚未分配角色模板" image={Empty.PRESENTED_IMAGE_SIMPLE} />}
      {isAdmin && user?.id !== selected.id && <Button onClick={() => {
        roleForm.resetFields()
        roleForm.setFieldsValue({ scope_type: 'global' })
        setEditor('role')
      }}>分配角色</Button>}
    </Space> },
    { key: 'resources', label: '资源授权', children: <Form form={resourceForm} layout="vertical" className="people-detail-content"
      onFinish={(values) => runPersonChange(() => editResources(selected.id, {
        revision: selected.revision, agent_ids: values.agent_ids || [],
        skill_ids: values.skills?.skill || [], package_ids: values.skills?.package || [],
      }))}>
      {grantError && <Alert type="error" message={grantError} showIcon />}
      <Form.Item name="agent_ids" label="助手授权">
        <Select mode="multiple" showSearch optionFilterProp="label" disabled={!isAdmin || !!grantError}
          options={agentOptions.map((agent) => ({ value: agent.id, label: agent.name, disabled: !agent.is_active }))} />
      </Form.Item>
      <Form.Item name="skills" label="技能授权">
        <SkillGrantSelect options={skillOptions} disabled={!isAdmin || !!grantError || !skillOptions} />
      </Form.Item>
      {isAdmin && <ModelExtraGrants key={`${selected.id}:${selected.revision}`} userId={selected.id}
        onSaved={() => afterPersonChange(selected.id)} />}
      {isAdmin && user?.id !== selected.id && <Button type="primary" htmlType="submit" loading={saving}
        disabled={!skillOptions || !!grantError}>保存资源授权</Button>}
    </Form> },
    { key: 'account', label: '账号与登录', children: <Space direction="vertical" className="people-detail-content">
      <Descriptions column={1} bordered size="small" items={[
        { key: 'account', label: '账号', children: selected.account }, { key: 'email', label: '绑定邮箱', children: selected.email || '未绑定' },
        { key: 'phone', label: '手机号', children: selected.phone || '未填写' },
        { key: 'created', label: '创建时间', children: selected.created_at ? new Date(selected.created_at).toLocaleString() : '—' },
        { key: 'login', label: '最近登录', children: selected.last_login ? new Date(selected.last_login).toLocaleString() : '暂无记录' },
      ]} />
      {isAdmin && <Space className="people-account-actions"><Button onClick={() => setAccountAction('edit')}>编辑账号资料</Button>
        {user?.id !== selected.id && <><Button onClick={() => setAccountAction('reset')}>重置登录密码</Button>
        <Button danger onClick={() => setAccountAction('delete')}>删除人员</Button></>}</Space>}
    </Space> },
  ] : []

  if (canView === null) return <div className="people-page"><Spin tip="正在加载人员权限" /></div>
  if (!canView) return <div className="people-page"><Alert type="warning" showIcon
    message="无法查看组织与人员" description={error || '请联系管理员申请组织与人员查看权限。'} /></div>

  return <div className="people-page">
    <header className="people-header">
      <div><h1>人员信息</h1><p>统一管理人员档案、账号登录、部门与角色授权。</p></div>
      <Space>
        <Button icon={<ReloadOutlined />} onClick={refresh}>刷新</Button>
        {isAdmin && <Button type="primary" icon={<TeamOutlined />} onClick={() => setAccountAction('create')}>新增人员</Button>}
      </Space>
    </header>
    <Tabs defaultActiveKey="people" items={[
      { key: 'people', label: '人员列表', children: <>
        <div className="people-filters">
          <Input.Search aria-label="搜索姓名、账号或邮箱" placeholder="搜索姓名、账号或邮箱"
            allowClear value={search} onChange={(event) => setSearch(event.target.value)}
            onSearch={(value) => changeFilter({ search: value.trim() })} />
          <Select aria-label="筛选部门" allowClear placeholder="全部部门" options={departmentOptions}
            value={filters.department_id} onChange={(value) => changeFilter({ department_id: value })} />
          <Select aria-label="筛选基础角色" allowClear placeholder="基础角色" value={filters.role}
            options={[{ value: 'admin', label: '管理员' }, { value: 'operator', label: '操作员' },
              { value: 'viewer', label: '查看者' }]} onChange={(value) => changeFilter({ role: value })} />
          <Select aria-label="筛选角色模板" allowClear placeholder="角色模板" value={filters.template_id}
            options={templates.map((role) => ({ value: role.id, label: role.name }))}
            onChange={(value) => changeFilter({ template_id: value })} />
          <Select aria-label="筛选状态" allowClear placeholder="全部状态" value={filters.status}
            options={[{ value: 'active', label: '启用' }, { value: 'disabled', label: '停用' },
              { value: 'departed', label: '已离职' }, { value: 'unknown', label: '未标记在职状态' }]}
            onChange={(value) => changeFilter({ status: value })} />
          <Select aria-label="排序" value={`${filters.sort_by}:${filters.sort_order}`}
            options={[{ value: 'last_login:desc', label: '最近登录' }, { value: 'created_at:desc', label: '最新创建' },
              { value: 'username:asc', label: '姓名 A-Z' }]}
            onChange={(value) => { const [sort_by, sort_order] = value.split(':'); changeFilter({ sort_by, sort_order }) }} />
        </div>
        {error && <Alert type="error" showIcon message={error} action={<Button size="small" onClick={refresh}>重试</Button>} />}
        <Table<Person> rowKey="id" loading={loading} dataSource={page.items} scroll={{ x: 800 }}
          locale={{ emptyText: '当前筛选条件下没有人员' }}
          onRow={(person) => ({ onClick: () => openPerson(person.id) })}
          pagination={{ current: page.page, pageSize: page.page_size, total: page.total,
            showSizeChanger: true, pageSizeOptions: [20, 50, 100],
            onChange: (index, size) => setFilters((current) => ({ ...current, page: index, page_size: size })) }}
          columns={[
            { title: '姓名 / 账号', key: 'name', render: (_, person) => <Button type="link" onClick={(event) => {
              event.stopPropagation(); openPerson(person.id)
            }}>{person.username}<span className="people-account">{person.account}</span></Button> },
            { title: '部门', dataIndex: 'department_name', render: (name: string | null) => name || '未分配' },
            { title: '角色', dataIndex: 'role', render: (role: string) => ROLE_LABELS[role] || role },
            { title: '状态', key: 'status', render: (_, person) => <Tag color={person.is_active ? 'green' : 'default'}>{displayStatus(person)}</Tag> },
            { title: '最近登录', dataIndex: 'last_login', render: (date: string | null) => date ? new Date(date).toLocaleDateString() : '—' },
          ]} />
      </> },
      { key: 'departments', label: '部门', children: <>
        {isAdmin && <Button type="primary" onClick={() => showDepartmentEditor(null)}>新建部门</Button>}
        <Table<Department> rowKey="id" dataSource={departments} pagination={false} scroll={{ x: 560 }}
          locale={{ emptyText: '暂无部门；现有人员不推断归属。' }} columns={[
            { title: '部门', dataIndex: 'name' }, { title: '代码', dataIndex: 'code' },
            { title: '上级', key: 'parent', render: (_, item) => departments.find((parent) => parent.id === item.parent_id)?.name || '—' },
            { title: '状态', dataIndex: 'is_active', render: (active: boolean) => active ? '启用' : '停用' },
            { title: '操作', key: 'actions', render: (_, item) => isAdmin ? <Space>
              <Button size="small" onClick={() => showDepartmentEditor(item)}>编辑</Button>
              <Button size="small" onClick={() => changeDepartmentActive(item)}>{item.is_active ? '停用' : '启用'}</Button>
            </Space> : '—' },
          ]} />
      </> },
      { key: 'templates', label: '角色模板', children: <>
        {isAdmin && <Button type="primary" onClick={() => showTemplateEditor(null)}>新建角色模板</Button>}
        <Table<RoleTemplate> rowKey="id" dataSource={templates} pagination={false} scroll={{ x: 560 }}
          locale={{ emptyText: '暂无角色模板；已有基础角色继续生效。' }} columns={[
            { title: '模板', dataIndex: 'name' }, { title: '代码', dataIndex: 'code' },
            { title: '权限', dataIndex: 'permissions', render: (codes: string[]) => permissionLabels(codes) },
            { title: '状态', dataIndex: 'is_active', render: (active: boolean) => active ? '启用' : '停用' },
            { title: '操作', key: 'actions', render: (_, item) => isAdmin ? <Space>
              <Button size="small" onClick={() => showTemplateEditor(item)}>编辑</Button>
              <Button size="small" onClick={() => changeTemplateActive(item)}>{item.is_active ? '停用' : '启用'}</Button>
            </Space> : '—' },
          ]} />
      </> },
    ]} />

    <Drawer title={selected?.username || '人员详情'} open={!!selected || detailLoading} width="min(720px, 100vw)"
      onClose={() => setSelected(null)} destroyOnClose loading={detailLoading}>
      {selected && <Tabs className="people-detail-tabs" activeKey={detailTab} onChange={setDetailTab} items={details} />}
    </Drawer>

    <AccountDialog action={accountAction} person={selected} onClose={() => setAccountAction(null)} onComplete={async (deleted) => {
      if (deleted) { setSelected(null); refresh() }
      else if (selected) await afterPersonChange(selected.id)
      else refresh()
    }} />
    <Modal title="编辑人员" open={editor === 'person'} onCancel={() => setEditor(null)}
      okText="保存" okButtonProps={{ loading: saving }} onOk={() => personForm.submit()} destroyOnClose>
      <Form form={personForm} layout="vertical" onFinish={(values) => {
        if (!selected) return
        const payload: Record<string, unknown> = { revision: selected.revision,
          job_title: values.job_title?.trim() || null }
        if (isAdmin) Object.assign(payload, {
          department_id: values.department_id ?? null, role: values.role,
          is_active: values.is_active, employment_status: values.employment_status ?? null,
          external_id: values.external_id?.trim() || null, source: values.source?.trim() || null,
        })
        runPersonChange(() => editPerson(selected.id, payload))
      }}>
        {isAdmin && <Form.Item name="department_id" label="部门"><Select allowClear options={departmentOptions} /></Form.Item>}
        <Form.Item name="job_title" label="职位"><Input maxLength={120} /></Form.Item>
        {isAdmin && <>
          <Form.Item name="role" label="基础角色"><Select disabled={selected?.id === user?.id}
            options={[{ value: 'admin', label: '管理员' }, { value: 'operator', label: '操作员' },
              { value: 'viewer', label: '查看者' }]} /></Form.Item>
          <Form.Item name="employment_status" label="在职状态"><Select allowClear
            options={[{ value: 'active', label: '在职' }, { value: 'departed', label: '离职（同时停用账号）' }]} /></Form.Item>
          <Form.Item name="is_active" label="账号状态"><Select disabled={selected?.id === user?.id}
            options={[{ value: true, label: '启用' }, { value: false, label: '停用' }]} /></Form.Item>
          <Form.Item name="source" label="外部身份来源（可留空）"><Input maxLength={80} /></Form.Item>
          <Form.Item name="external_id" label="外部身份标识（与来源同时填写）"><Input maxLength={160} /></Form.Item>
        </>}
      </Form>
    </Modal>

    <Modal title={editingDepartment ? '编辑部门' : '新建部门'} open={editor === 'department'}
      onCancel={() => setEditor(null)} okText="保存" okButtonProps={{ loading: saving }} onOk={saveDepartment} destroyOnClose>
      <Form form={departmentForm} layout="vertical">
        {!editingDepartment && <Form.Item name="code" label="部门代码" rules={[{ required: true, message: '请输入部门代码' }]}>
          <Input maxLength={80} placeholder="字母开头，如 finance_team" /></Form.Item>}
        <Form.Item name="name" label="部门名称" rules={[{ required: true, message: '请输入部门名称' }]}><Input maxLength={120} /></Form.Item>
        <Form.Item name="parent_id" label="上级部门"><Select allowClear options={departmentOptions.filter((item) => item.value !== editingDepartment?.id)} /></Form.Item>
      </Form>
    </Modal>

    <Modal title={editingTemplate ? '编辑角色模板' : '新建角色模板'} open={editor === 'template'}
      onCancel={() => setEditor(null)} okText="保存" okButtonProps={{ loading: saving }} onOk={saveTemplate} destroyOnClose>
      <Form form={templateForm} layout="vertical">
        {!editingTemplate && <Form.Item name="code" label="模板代码" rules={[{ required: true, message: '请输入模板代码' }]}><Input maxLength={80} /></Form.Item>}
        <Form.Item name="name" label="模板名称" rules={[{ required: true, message: '请输入模板名称' }]}><Input maxLength={120} /></Form.Item>
        <Form.Item label="权限路径"><div className="people-permission-tree"><Tree checkable selectable={false}
          treeData={buildPermTreeData()} checkedKeys={permChecked}
          onCheck={(keys) => setPermChecked(keys as string[])} /></div></Form.Item>
      </Form>
    </Modal>

    <Modal title="分配角色" open={editor === 'role'} onCancel={() => setEditor(null)}
      okText="分配并撤销旧会话" okButtonProps={{ loading: saving }} onOk={grantRole} destroyOnClose>
      <Form form={roleForm} layout="vertical">
        <Form.Item name="template_id" label="角色模板" rules={[{ required: true, message: '请选择模板' }]}>
          <Select options={templates.filter((template) => template.is_active).map((template) => ({ value: template.id, label: template.name }))} />
        </Form.Item>
        <Form.Item name="scope_type" label="数据范围" rules={[{ required: true, message: '请选择范围' }]}>
          <Select options={[{ value: 'global', label: '全部数据' }, { value: 'department', label: '该人员所在部门' },
            { value: 'departments', label: '指定部门' }, { value: 'self', label: '本人' },
            { value: 'resource', label: '指定资源' }]} />
        </Form.Item>
        {scopeType === 'departments' && <Form.Item name="department_ids" label="指定部门"
          rules={[{ required: true, type: 'array', min: 1, message: '至少选择一个部门' }]}>
          <Select mode="multiple" options={departmentOptions} />
        </Form.Item>}
        {scopeType === 'resource' && <>
          <Form.Item name="resource_type" label="资源类型" rules={[{ required: true }]}><Select
            options={[{ value: 'user', label: '人员' }, { value: 'agent', label: '助手' },
              { value: 'skill', label: '技能' }, { value: 'package', label: '技能包' }]} /></Form.Item>
          <Form.Item name="resource_ids" label="资源编号（逗号分隔）" rules={[{ required: true }]}>
            <Input placeholder="例如 12, 34" /></Form.Item>
        </>}
      </Form>
    </Modal>
  </div>
}
