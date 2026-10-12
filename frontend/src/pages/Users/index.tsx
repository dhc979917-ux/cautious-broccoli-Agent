import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Button, Form, Input, Modal, Radio, Segmented, Select, Space, Table, Tag, Tooltip, Tree, message, Alert, Descriptions,
} from 'antd'
import {
  TeamOutlined, EditOutlined, StopOutlined, CheckCircleOutlined, SearchOutlined,
  CaretUpOutlined, CaretDownOutlined, PlusOutlined, DeleteOutlined, ExclamationCircleOutlined,
  SyncOutlined,
} from '@ant-design/icons'
import { getAllUsersSorted, updateUser, getAllAgents, AdminUserInfo, hardDeleteUser, resetUserPassword, getSkillGrantOptions, SkillGrantOptions } from '../../api/admin'
import { registerUser } from '../../api/auth'
import { getUsersOverview, UserOverview } from '../../api/monitoring'
import { ROLE_COLORS, ROLE_LABELS } from '../../constants/roles'
import { buildPermTreeData, expandToCheckedKeys, collapseToCodes } from '../../constants/permissions'
import './index.css'

interface AgentOption { id: number; name: string; description?: string; is_active: boolean }

// 后端 users 表支持排序的字段；其余列（如本周 Token）走客户端排序
const SERVER_SORT_FIELDS = ['last_login']

function fmtTokens(n: number): string {
  if (!n) return '0'
  if (n >= 1000000) return `${(n / 1000000).toFixed(1)}M`
  if (n >= 1000) return `${(n / 1000).toFixed(1)}K`
  return String(n)
}

/** 集合等价比较：顺序差异不算变化（后端同样只按集合判断是否需要吊销会话） */
function sameSet(a: (string | number)[], b: (string | number)[]): boolean {
  if (a.length !== b.length) return false
  const pool = new Set(b.map(String))
  return a.every((item) => pool.has(String(item)))
}

export default function Users() {
  const [users, setUsers] = useState<AdminUserInfo[]>([])
  const [usageMap, setUsageMap] = useState<Record<number, UserOverview>>({})
  const [agents, setAgents] = useState<AgentOption[]>([])
  // Skill 授权树选项（包=父节点勾选授权整包；成员=子节点单独授权）
  const [grantOptions, setGrantOptions] = useState<SkillGrantOptions | null>(null)
  const [loading, setLoading] = useState(false)
  const [searchText, setSearchText] = useState('')
  const [roleFilter, setRoleFilter] = useState<string | null>(null)
  // 默认按最后登录倒序
  const [sortBy, setSortBy] = useState('last_login')
  const [sortOrder, setSortOrder] = useState('desc')

  // 编辑弹窗（角色/邮箱/Agent 授权/功能权限合并为一个）
  const [editUser, setEditUser] = useState<AdminUserInfo | null>(null)
  const [editSaving, setEditSaving] = useState(false)
  const [editForm] = Form.useForm()
  const editRole = Form.useWatch('role', editForm)
  // 功能权限树勾选（受控）：回显时展开存储码，保存时收敛为路径码
  const [permChecked, setPermChecked] = useState<string[]>([])
  const permTreeData = useMemo(() => buildPermTreeData(), [])
  // Skill 授权勾选（受控）：key 编码 p-<pkgId> / s-<skillId>
  const [grantChecked, setGrantChecked] = useState<string[]>([])
  const skillGrantTree = useMemo(() => {
    if (!grantOptions) return []
    const byPkg = new Map<number, { title: string; key: string; isLeaf: boolean }[]>()
    const roots: { title: string; key: string; children?: { title: string; key: string; isLeaf: boolean }[] }[] = []
    for (const s of grantOptions.skills) {
      const node = { title: `${s.label}${s.is_hidden ? '（隐藏）' : ''}`, key: `s-${s.id}`, isLeaf: true }
      if (s.package_id == null) {
        roots.push(node)
      } else {
        const arr = byPkg.get(s.package_id) || []
        arr.push(node)
        byPkg.set(s.package_id, arr)
      }
    }
    for (const p of grantOptions.packages) {
      const children = byPkg.get(p.id) || []
      roots.push({
        title: `${p.label}${p.is_hidden ? '（隐藏）' : ''}`,
        key: `p-${p.id}`,
        ...(children.length ? { children } : {}),
      })
    }
    return roots
  }, [grantOptions])

  // 新建用户弹窗（复用登录页注册逻辑）
  const [createOpen, setCreateOpen] = useState(false)
  const [createLoading, setCreateLoading] = useState(false)
  const [createForm] = Form.useForm()

  // 彻底删除两步确认弹窗（复用 UserAdmin 交互模式）
  const [destroyOpen, setDestroyOpen] = useState(false)
  const [destroyTarget, setDestroyTarget] = useState<AdminUserInfo | null>(null)
  const [destroyStep, setDestroyStep] = useState<1 | 2>(1)
  const [destroyUsername, setDestroyUsername] = useState('')
  const [destroyPassword, setDestroyPassword] = useState('')
  const [destroyLoading, setDestroyLoading] = useState(false)

  // 重置密码两步确认弹窗（复用彻底删除的交互模式）
  const [resetOpen, setResetOpen] = useState(false)
  const [resetTarget, setResetTarget] = useState<AdminUserInfo | null>(null)
  const [resetStep, setResetStep] = useState<1 | 2>(1)
  const [resetUsername, setResetUsername] = useState('')
  const [resetPassword, setResetPassword] = useState('')
  const [resetLoading, setResetLoading] = useState(false)

  const fetchUsers = useCallback(async () => {
    setLoading(true)
    try {
      // 后端不支持排序的字段退回 last_login 服务端排序，该列本身在客户端排序
      const field = SERVER_SORT_FIELDS.includes(sortBy) ? sortBy : 'last_login'
      setUsers(await getAllUsersSorted(field, sortOrder))
    } catch {
      message.error('获取用户列表失败')
    } finally {
      setLoading(false)
    }
  }, [sortBy, sortOrder])

  const fetchUsage = useCallback(async () => {
    try {
      const list = await getUsersOverview()
      const m: Record<number, UserOverview> = {}
      list.forEach((u) => { m[u.user_id] = u })
      setUsageMap(m)
    } catch { /* 用量列显示为 0 */ }
  }, [])

  const fetchAgents = useCallback(async () => {
    try { setAgents(await getAllAgents()) } catch { /* 授权列表显示为空 */ }
  }, [])

  const fetchGrantOptions = useCallback(async () => {
    try { setGrantOptions(await getSkillGrantOptions()) } catch { /* 授权树显示为空 */ }
  }, [])

  useEffect(() => { fetchUsers(); fetchUsage(); fetchAgents(); fetchGrantOptions() }, [fetchUsers, fetchUsage, fetchAgents, fetchGrantOptions])

  // ===== 新建用户（与登录页注册同字段同校验）=====
  const handleCreate = async (values: any) => {
    setCreateLoading(true)
    try {
      await registerUser({
        account: values.account,
        username: values.username,
        password: values.password,
        phone: values.phone,
        email: values.email || null,
      })
      message.success('用户创建成功')
      setCreateOpen(false)
      createForm.resetFields()
      fetchUsers()
    } catch (e: any) {
      message.error(e.response?.data?.detail || '创建失败')
    } finally {
      setCreateLoading(false)
    }
  }

  // ===== 编辑（角色/邮箱/Agent 授权/功能权限）=====
  const openEdit = (u: AdminUserInfo) => {
    setEditUser(u)
    editForm.setFieldsValue({
      username: u.username || '',
      account: u.account || '',
      role: u.role,
      agent_ids: u.agent_ids || [],
    })
    setPermChecked(expandToCheckedKeys(u.permissions || []))
    setGrantChecked([
      ...(u.skill_grant_package_ids || []).map((id) => `p-${id}`),
      ...(u.skill_grant_skill_ids || []).map((id) => `s-${id}`),
    ])
  }

  const handleEditSave = async (values: { username: string; account: string; role: string; agent_ids?: number[] }) => {
    if (!editUser) return
    setEditSaving(true)
    try {
      const skillGrantPackageIds = grantChecked.filter((k) => k.startsWith('p-')).map((k) => Number(k.slice(2)))
      const skillGrantSkillIds = grantChecked.filter((k) => k.startsWith('s-')).map((k) => Number(k.slice(2)))
      const payload: { username: string; account: string; role: string; agent_ids: number[]; permissions: string[]; skill_grant_skill_ids: number[]; skill_grant_package_ids: number[] } = {
        username: values.username.trim(),
        account: values.account.trim(),
        role: values.role,
        agent_ids: values.agent_ids || [],
        // admin 天然全权限直接清空；非 admin 把树的勾选收敛为路径码（子树全选收敛为父码）
        permissions: values.role === 'admin' ? [] : collapseToCodes(permChecked),
        // Skill 授权实时生效（后端逐请求校验），不触发强制下线
        skill_grant_skill_ids: skillGrantSkillIds,
        skill_grant_package_ids: skillGrantPackageIds,
      }
      await updateUser(editUser.id, payload)
      // 角色 / 功能权限 / Agent 授权任一变化，后端会吊销该用户的会话：
      // 旧 token 立刻失效 + 停掉在跑的 Agent 任务 + 推送到登录页，重登后新权限生效。
      // 明确提示管理员，避免误以为"改了没生效"。
      const permChanged =
        payload.role !== editUser.role ||
        !sameSet(payload.permissions, editUser.permissions || []) ||
        !sameSet(payload.agent_ids, editUser.agent_ids || [])
      message.success(permChanged
        ? '用户已更新，该用户已被强制下线，重新登录后新权限生效'
        : '用户已更新')
      setEditUser(null)
      fetchUsers()
    } catch (e: any) {
      message.error(e.response?.data?.detail || '更新失败')
    } finally {
      setEditSaving(false)
    }
  }

  const handleToggleActive = async (u: AdminUserInfo) => {
    try {
      await updateUser(u.id, { is_active: !u.is_active })
      message.success(u.is_active ? '账号已禁用，该用户已被强制下线' : '账号已启用')
      fetchUsers()
    } catch (e: any) {
      message.error(e.response?.data?.detail || '操作失败')
    }
  }

  // ===== 彻底删除两步确认 =====
  const openDestroy = (u: AdminUserInfo) => {
    setDestroyTarget(u)
    setDestroyStep(1)
    setDestroyUsername('')
    setDestroyPassword('')
    setDestroyOpen(true)
  }

  const closeDestroy = () => {
    setDestroyOpen(false)
    setDestroyTarget(null)
  }

  const handleDestroyConfirm = async () => {
    if (!destroyTarget) return
    setDestroyLoading(true)
    try {
      await hardDeleteUser(destroyTarget.id, destroyUsername, destroyPassword)
      message.success('用户已彻底删除')
      closeDestroy()
      fetchUsers()
    } catch (e: any) {
      message.error(e.response?.data?.detail || '删除失败')
    } finally {
      setDestroyLoading(false)
    }
  }

  // ===== 重置密码为默认密码（两步确认）=====
  const openReset = (u: AdminUserInfo) => {
    setResetTarget(u)
    setResetStep(1)
    setResetUsername('')
    setResetPassword('')
    setResetOpen(true)
  }

  const closeReset = () => {
    setResetOpen(false)
    setResetTarget(null)
  }

  const handleResetConfirm = async () => {
    if (!resetTarget) return
    setResetLoading(true)
    try {
      await resetUserPassword(resetTarget.id, resetUsername, resetPassword)
      message.success('密码已重置为默认密码')
      closeReset()
    } catch (e: any) {
      message.error(e.response?.data?.detail || '重置失败')
    } finally {
      setResetLoading(false)
    }
  }

  // ===== 排序：自绘表头点击，完全不依赖 antd sorter 回调 =====
  // 三态循环：点新列降序 → 升序 → 中间态（无激活列，回落默认排序）→ 再点降序
  const toggleSort = (field: string) => {
    if (sortBy === field) {
      if (sortOrder === 'desc') {
        setSortOrder('asc')
      } else {
        // 升序再点 → 中间态：清空激活列，数据回落默认"最后登录倒序"
        setSortBy('')
        setSortOrder('desc')
      }
    } else {
      setSortBy(field)
      setSortOrder('desc')
    }
  }

  const sortTitle = (label: string, field: string) => (
    <span className="users-sort-title" onClick={() => toggleSort(field)}>
      {label}
      <span className="users-sort-arrows">
        <CaretUpOutlined className={sortBy === field && sortOrder === 'asc' ? 'active' : ''} />
        <CaretDownOutlined className={sortBy === field && sortOrder === 'desc' ? 'active' : ''} />
      </span>
    </span>
  )

  const dataSource = useMemo(() => {
    const rows = users.filter((u) => {
      const matchSearch = !searchText || u.username.toLowerCase().includes(searchText.toLowerCase())
      const matchRole = !roleFilter || u.role === roleFilter
      return matchSearch && matchRole
    })
    // 本周 Token 来自监控聚合数据（不在 users 表），客户端排序
    if (sortBy === 'week_tokens') {
      rows.sort((a, b) => {
        const d = (usageMap[a.id]?.week_tokens ?? 0) - (usageMap[b.id]?.week_tokens ?? 0)
        return sortOrder === 'asc' ? d : -d
      })
    }
    return rows
  }, [users, searchText, roleFilter, sortBy, sortOrder, usageMap])

  const columns = [
    {
      title: '用户名', dataIndex: 'username', key: 'username', width: 120, align: 'center' as const,
      render: (t: string) => <span style={{ fontWeight: 600 }}>{t}</span>,
    },
    {
      title: '账号', dataIndex: 'account', key: 'account', width: 120, align: 'center' as const,
      render: (t?: string) => t || <span style={{ color: 'var(--text-tertiary)' }}>—</span>,
    },
    {
      title: '角色', dataIndex: 'role', key: 'role', width: 90, align: 'center' as const,
      render: (t: string) => <Tag color={ROLE_COLORS[t]}>{ROLE_LABELS[t] || t}</Tag>,
    },
    {
      title: '手机号', dataIndex: 'phone', key: 'phone', width: 130, align: 'center' as const,
      render: (t?: string) => t || <span style={{ color: 'var(--text-tertiary)' }}>—</span>,
    },
    {
      title: '状态', key: 'is_active', width: 80, align: 'center' as const,
      render: (_: any, r: AdminUserInfo) => r.is_active ? <Tag color="success">启用</Tag> : <Tag>已禁用</Tag>,
    },
    {
      title: sortTitle('本周 Token', 'week_tokens'), key: 'week_tokens', width: 120, align: 'center' as const,
      render: (_: any, r: AdminUserInfo) => fmtTokens(usageMap[r.id]?.week_tokens ?? 0),
    },
    {
      title: sortTitle('最后登录', 'last_login'), dataIndex: 'last_login', key: 'last_login', width: 170, align: 'center' as const,
      render: (t?: string) => t ? new Date(t).toLocaleString('zh-CN', { hour12: false }) : '—',
    },
    {
      title: '操作', key: 'action', width: 70, align: 'center' as const,
      render: (_: any, r: AdminUserInfo) => (
        <Space size={0}>
          <Tooltip title="编辑（用户名/账号/角色/授权/权限）"><Button type="text" size="small" icon={<EditOutlined />} onClick={() => openEdit(r)} /></Tooltip>
          <Tooltip title="重置密码为默认密码"><Button type="text" size="small" icon={<SyncOutlined />} onClick={() => openReset(r)} /></Tooltip>
          {r.is_active
            ? <Tooltip title="禁用账号"><Button type="text" size="small" danger icon={<StopOutlined />} onClick={() => handleToggleActive(r)} /></Tooltip>
            : <Tooltip title="启用账号"><Button type="text" size="small" icon={<CheckCircleOutlined />} onClick={() => handleToggleActive(r)} /></Tooltip>}
          <Tooltip title="彻底删除（不可恢复）"><Button type="text" size="small" danger icon={<DeleteOutlined />} onClick={() => openDestroy(r)} /></Tooltip>
        </Space>
      ),
    },
  ]

  return (
    <div className="users-page">
      <div className="users-header">
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <div className="users-title"><TeamOutlined /> 人员信息</div>
          <div
            onClick={() => setCreateOpen(true)}
            style={{
              background: 'var(--color-primary)',
              color: '#fff',
              borderRadius: 8,
              height: 30,
              padding: '0 14px',
              fontSize: 14,
              fontWeight: 600,
              cursor: 'pointer',
              display: 'flex',
              alignItems: 'center',
              gap: 6,
              boxShadow: '0 1px 2px rgba(26, 29, 33, 0.10)',
            }}
          >
            <PlusOutlined /> 新建用户
          </div>
        </div>
        <div className="users-toolbar">
          <Input
            allowClear
            prefix={<SearchOutlined />}
            placeholder="搜索用户名"
            value={searchText}
            onChange={(e) => setSearchText(e.target.value)}
            style={{ width: 200 }}
          />
          <Segmented
            value={roleFilter || 'all'}
            onChange={(v) => setRoleFilter(v === 'all' ? null : String(v))}
            options={[
              { label: '全部', value: 'all' },
              { label: '管理员', value: 'admin' },
              { label: '操作员', value: 'operator' },
              { label: '查看者', value: 'viewer' },
            ]}
          />
          <span className="users-count">共 {dataSource.length} 人</span>
        </div>
      </div>

      <Table
        rowKey="id"
        size="small"
        loading={loading}
        tableLayout="fixed"
        columns={columns as any}
        dataSource={dataSource}
        pagination={{ pageSize: 18, showSizeChanger: false }}
      />

      {/* 编辑弹窗：角色 + 邮箱 + 功能权限 + Agent 授权 + Skill 授权 */}
      <Modal
        title={`编辑用户 · ${editUser?.username || ''}`}
        open={!!editUser}
        onCancel={() => setEditUser(null)}
        onOk={() => editForm.submit()}
        confirmLoading={editSaving}
        okText="保存"
        cancelText="取消"
      >
        <Form form={editForm} layout="vertical" onFinish={handleEditSave}>
          <Form.Item name="username" label="用户名" rules={[{ required: true, message: '请输入用户名（真实姓名）' }]}>
            <Input placeholder="真实姓名" />
          </Form.Item>
          <Form.Item name="account" label="账号" rules={[{ required: true, message: '请输入账号' }]}>
            <Input placeholder="登录用账号" />
          </Form.Item>
          <Form.Item name="role" label="角色" rules={[{ required: true }]}>
            <Radio.Group>
              <Radio value="admin">管理员</Radio>
              <Radio value="operator">操作员</Radio>
              <Radio value="viewer">查看者</Radio>
            </Radio.Group>
          </Form.Item>
          <Form.Item label="功能权限" className="users-edit-group">
            {editRole === 'admin' ? (
              <div className="users-perm-hint">管理员默认拥有全部权限，无需单独分配</div>
            ) : (
              <>
                <div className="users-perm-hint">勾选父节点 = 全选其下所有子功能；取消 = 全不选；支持逐层部分选择</div>
                <div className="users-perm-tree">
                  <Tree
                    checkable
                    selectable={false}
                    treeData={permTreeData}
                    checkedKeys={permChecked}
                    onCheck={(keys) => setPermChecked(keys as string[])}
                  />
                </div>
              </>
            )}
          </Form.Item>
          <Form.Item label="Agent 授权" className="users-edit-group">
            <div className="users-perm-hint">可按名字搜索；下拉列表固定高度内滚动（管理员默认可用全部）</div>
            <Form.Item name="agent_ids" noStyle>
              <Select
                mode="multiple"
                showSearch
                placeholder="搜索并选择可对话的 Agent"
                optionFilterProp="label"
                listHeight={160}
                maxTagCount={5}
                options={agents.map((a) => ({
                  value: a.id,
                  label: a.is_active ? a.name : `${a.name}（已停用）`,
                }))}
                filterOption={(input, option) =>
                  String(option?.label ?? '').toLowerCase().includes(input.toLowerCase())
                }
              />
            </Form.Item>
          </Form.Item>
          <Form.Item label="Skill 授权" className="users-edit-group">
            <div className="users-perm-hint">
              勾选技能包=授权整包（包内后续新增技能自动生效）；勾选包内单个技能=仅授权该技能。
              授权会覆盖角色门槛与广场隐藏：被授权用户在技能广场和聊天技能列表均可见可用。
            </div>
            <div className="users-perm-tree">
              <Tree
                checkable
                checkStrictly
                selectable={false}
                treeData={skillGrantTree}
                checkedKeys={{ checked: grantChecked, halfChecked: [] }}
                onCheck={(keys) => setGrantChecked(((keys as { checked: (string | number)[] }).checked).map(String))}
              />
            </div>
          </Form.Item>
        </Form>
      </Modal>

      {/* 新建用户弹窗 */}
      <Modal
        title="新建用户"
        open={createOpen}
        onCancel={() => setCreateOpen(false)}
        onOk={() => createForm.submit()}
        confirmLoading={createLoading}
        okText="创建"
        cancelText="取消"
        destroyOnClose
      >
        <Form form={createForm} layout="vertical" onFinish={handleCreate} preserve={false}>
          <Form.Item name="account" label="账号" rules={[{ required: true, message: '请输入账号' }]}>
            <Input placeholder="登录用账号" />
          </Form.Item>
          <Alert type="info" showIcon message="新用户初始密码统一为 Initial@123456，首次登录必须修改。" style={{ marginBottom: 16 }} />
          <Form.Item name="password" label="初始密码" initialValue="Initial@123456" rules={[{ required: true }]}>
            <Input.Password disabled />
          </Form.Item>
          <Form.Item name="username" label="用户名" rules={[{ required: true, message: '请输入用户名（真实姓名）' }]}>
            <Input placeholder="真实姓名" />
          </Form.Item>
          <Form.Item name="phone" label="手机号" rules={[{ required: true, message: '请输入手机号' }, { pattern: /^\d{11}$/, message: '手机号必须为11位数字' }]}>
            <Input placeholder="11位手机号" maxLength={11} />
          </Form.Item>
          <Form.Item name="email" label="邮箱（可选）" rules={[{ type: 'email', message: '邮箱格式不正确' }]}>
            <Input placeholder="邮箱" />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={<span style={{ color: '#ef4444' }}><ExclamationCircleOutlined style={{ marginRight: 8 }} />彻底删除用户</span>}
        open={destroyOpen}
        onCancel={closeDestroy}
        closable={false}
        footer={destroyStep === 1 ? (
          <Space>
            <Button onClick={closeDestroy}>取消</Button>
            <Button type="primary" danger disabled={!destroyUsername || !destroyPassword} onClick={() => setDestroyStep(2)}>下一步</Button>
          </Space>
        ) : (
          <Space>
            <Button onClick={() => setDestroyStep(1)}>返回</Button>
            <Button type="primary" danger loading={destroyLoading} onClick={handleDestroyConfirm}>确认删除</Button>
          </Space>
        )}
      >
        <Alert
          message="危险操作！此操作不可恢复！"
          description="彻底删除将级联删除该用户的会话、消息、授权与文件记录，知识库与 MCP 服务的创建人信息将被置空。"
          type="error"
          showIcon
          style={{ marginBottom: 16, borderRadius: 8 }}
        />
        {destroyStep === 1 ? (
          <div>
            <p style={{ marginBottom: 12 }}>请输入管理员账号和密码以继续：</p>
            <Input placeholder="管理员账号" value={destroyUsername} onChange={(e) => setDestroyUsername(e.target.value)} style={{ marginBottom: 8, borderRadius: 8 }} size="large" />
            <Input.Password placeholder="管理员密码" value={destroyPassword} onChange={(e) => setDestroyPassword(e.target.value)} size="large" style={{ borderRadius: 8 }} onPressEnter={() => { if (destroyUsername && destroyPassword) setDestroyStep(2) }} />
          </div>
        ) : (
          <div>
            <Alert message="最终确认" type="warning" showIcon style={{ marginBottom: 16, borderRadius: 8 }} />
            <Descriptions bordered size="small" column={1}>
              <Descriptions.Item label="操作">彻底删除（不可恢复）</Descriptions.Item>
              <Descriptions.Item label="用户"><strong style={{ color: '#ef4444' }}>{destroyTarget?.username}</strong></Descriptions.Item>
              <Descriptions.Item label="管理员账号">{destroyUsername}</Descriptions.Item>
            </Descriptions>
            <p style={{ marginTop: 12, color: '#ef4444', fontWeight: 600 }}>点击"确认删除"后，数据将永久丢失！</p>
          </div>
        )}
      </Modal>

      <Modal
        title={<span style={{ color: '#d97706' }}><SyncOutlined style={{ marginRight: 8 }} />重置用户密码</span>}
        open={resetOpen}
        onCancel={closeReset}
        closable={false}
        footer={resetStep === 1 ? (
          <Space>
            <Button onClick={closeReset}>取消</Button>
            <Button type="primary" disabled={!resetUsername || !resetPassword} onClick={() => setResetStep(2)}>下一步</Button>
          </Space>
        ) : (
          <Space>
            <Button onClick={() => setResetStep(1)}>返回</Button>
            <Button type="primary" loading={resetLoading} onClick={handleResetConfirm}>确认重置</Button>
          </Space>
        )}
      >
        <Alert
          message="将该用户的登录密码重置为默认密码"
          description="重置后该用户需使用默认密码 Initial@123456 登录，请通知其尽快修改。"
          type="warning"
          showIcon
          style={{ marginBottom: 16, borderRadius: 8 }}
        />
        {resetStep === 1 ? (
          <div>
            <p style={{ marginBottom: 12 }}>请输入管理员账号和密码以继续：</p>
            <Input placeholder="管理员账号" value={resetUsername} onChange={(e) => setResetUsername(e.target.value)} style={{ marginBottom: 8, borderRadius: 8 }} size="large" />
            <Input.Password placeholder="管理员密码" value={resetPassword} onChange={(e) => setResetPassword(e.target.value)} size="large" style={{ borderRadius: 8 }} onPressEnter={() => { if (resetUsername && resetPassword) setResetStep(2) }} />
          </div>
        ) : (
          <div>
            <Alert message="最终确认" type="warning" showIcon style={{ marginBottom: 16, borderRadius: 8 }} />
            <Descriptions bordered size="small" column={1}>
              <Descriptions.Item label="操作">重置密码为默认密码</Descriptions.Item>
              <Descriptions.Item label="用户"><strong style={{ color: '#d97706' }}>{resetTarget?.username}</strong></Descriptions.Item>
              <Descriptions.Item label="管理员账号">{resetUsername}</Descriptions.Item>
            </Descriptions>
          </div>
        )}
      </Modal>
    </div>
  )
}
