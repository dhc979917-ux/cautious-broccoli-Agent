import { useState, useEffect } from 'react'
import { Button, Space, Table, Tag, Modal, Form, Input, Radio, Segmented, message, Tooltip, Checkbox, Alert, Descriptions } from 'antd'
import {
  ArrowLeftOutlined,
  EditOutlined,
  StopOutlined,
  CheckCircleOutlined,
  KeyOutlined,
  UserOutlined,
  BulbOutlined,
  SearchOutlined,
  PlusOutlined,
  DeleteOutlined,
  ExclamationCircleOutlined,
} from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'
import { getAllUsers, updateUser, AdminUserInfo, getAllAgents, hardDeleteUser } from '../../api/admin'
import { registerUser } from '../../api/auth'
import { useAuthStore } from '../../store/authStore'
import PlatformLogo from '../../components/PlatformLogo'
import { ROLE_COLORS, ROLE_LABELS } from '../../constants/roles'
import './index.css'

interface AgentOption {
  id: number
  name: string
  description?: string
  is_active: boolean
}

export default function UserAdmin() {
  const navigate = useNavigate()
  const { theme, toggleTheme } = useAuthStore()

  const [users, setUsers] = useState<AdminUserInfo[]>([])
  const [agents, setAgents] = useState<AgentOption[]>([])
  const [loading, setLoading] = useState(false)
  const [searchText, setSearchText] = useState('')
  const [roleFilter, setRoleFilter] = useState<string | null>(null)

  // 编辑用户（邮箱/角色）弹窗
  const [editUser, setEditUser] = useState<AdminUserInfo | null>(null)
  const [editSaving, setEditSaving] = useState(false)
  const [editForm] = Form.useForm()

  // Agent 授权弹窗
  const [permUser, setPermUser] = useState<AdminUserInfo | null>(null)
  const [permAgentIds, setPermAgentIds] = useState<number[]>([])
  const [permSaving, setPermSaving] = useState(false)

  // 新建用户弹窗（复用登录页注册逻辑）
  const [createOpen, setCreateOpen] = useState(false)
  const [createLoading, setCreateLoading] = useState(false)
  const [createForm] = Form.useForm()

  // 彻底删除两步确认弹窗（复用 Admin 页交互模式）
  const [destroyOpen, setDestroyOpen] = useState(false)
  const [destroyTarget, setDestroyTarget] = useState<AdminUserInfo | null>(null)
  const [destroyStep, setDestroyStep] = useState<1 | 2>(1)
  const [destroyUsername, setDestroyUsername] = useState('')
  const [destroyPassword, setDestroyPassword] = useState('')
  const [destroyLoading, setDestroyLoading] = useState(false)

  useEffect(() => { fetchUsers(); fetchAgents() }, [])

  const fetchUsers = async () => {
    setLoading(true)
    try {
      setUsers(await getAllUsers())
    } catch {
      message.error('获取用户列表失败')
    } finally {
      setLoading(false)
    }
  }

  const fetchAgents = async () => {
    try {
      setAgents(await getAllAgents())
    } catch { /* 授权弹窗里会显示为空 */ }
  }

  // ===== 编辑用户（邮箱/角色）=====
  const openEdit = (u: AdminUserInfo) => {
    setEditUser(u)
  }

  const handleEditSave = async (values: { email?: string; role: string }) => {
    if (!editUser) return
    setEditSaving(true)
    try {
      const payload: { role: string; email?: string } = { role: values.role }
      if (values.email?.trim()) payload.email = values.email.trim()
      await updateUser(editUser.id, payload)
      message.success('用户已更新')
      setEditUser(null)
      fetchUsers()
    } catch (e: any) {
      message.error(e.response?.data?.detail || '更新失败')
    } finally {
      setEditSaving(false)
    }
  }

  // ===== 启用/禁用 =====
  const handleToggleActive = async (u: AdminUserInfo) => {
    try {
      await updateUser(u.id, { is_active: !u.is_active })
      message.success(u.is_active ? '账号已禁用' : '账号已启用')
      fetchUsers()
    } catch (e: any) {
      message.error(e.response?.data?.detail || '操作失败')
    }
  }

  // ===== Agent 授权 =====
  const openPerm = (u: AdminUserInfo) => {
    setPermUser(u)
    setPermAgentIds(u.agent_ids || [])
  }

  const handlePermSave = async () => {
    if (!permUser) return
    setPermSaving(true)
    try {
      await updateUser(permUser.id, { agent_ids: permAgentIds })
      message.success('授权已更新')
      setPermUser(null)
      fetchUsers()
    } catch (e: any) {
      message.error(e.response?.data?.detail || '更新失败')
    } finally {
      setPermSaving(false)
    }
  }

  // ===== 新建用户（复用注册接口）=====
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
      fetchUsers()
    } catch (e: any) {
      message.error(e.response?.data?.detail || '创建失败')
    } finally {
      setCreateLoading(false)
    }
  }

  // ===== 彻底删除（密码复核，不可恢复）=====
  const openDestroy = (u: AdminUserInfo) => {
    setDestroyTarget(u)
    setDestroyOpen(true)
  }

  const closeDestroy = () => {
    setDestroyOpen(false)
    setDestroyStep(1)
    setDestroyUsername('')
    setDestroyPassword('')
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

  // ===== 搜索 + 角色筛选 =====
  const filteredUsers = users.filter((u) => {
    const matchSearch = !searchText || u.username.toLowerCase().includes(searchText.toLowerCase())
    const matchRole = !roleFilter || u.role === roleFilter
    return matchSearch && matchRole
  })

  const activeCount = users.filter((u) => u.is_active).length

  const columns = [
    { title: '序号', key: 'seq', width: 60, render: (_: any, _r: AdminUserInfo, index: number) => index + 1 },
    {
      title: '用户名', dataIndex: 'username', key: 'username',
      render: (t: string) => <span style={{ fontWeight: 500 }}>{t}</span>,
    },
    {
      title: '邮箱', dataIndex: 'email', key: 'email',
      render: (t: string) => t || '-',
    },
    {
      title: '角色', dataIndex: 'role', key: 'role', width: 100,
      render: (t: string) => <Tag color={ROLE_COLORS[t]}>{ROLE_LABELS[t] || t}</Tag>,
    },
    {
      title: '手机号', dataIndex: 'phone', key: 'phone', width: 140,
      render: (t?: string) => t || '-',
    },
    {
      title: '状态', key: 'is_active', width: 90,
      render: (_: any, r: AdminUserInfo) => (r.is_active ? <Tag color="success">启用</Tag> : <Tag>已禁用</Tag>),
    },
    {
      title: '最后登录', dataIndex: 'last_login', key: 'last_login', width: 160,
      render: (t: string) => (t ? new Date(t).toLocaleString('zh-CN', { hour12: false }) : '-'),
    },
    {
      title: '操作', key: 'action', width: 150,
      render: (_: any, r: AdminUserInfo) => (
        <Space size="small">
          <Tooltip title="编辑（邮箱/角色）">
            <Button type="text" size="small" icon={<EditOutlined />} onClick={() => openEdit(r)} />
          </Tooltip>
          <Tooltip title="Agent授权">
            <Button type="text" size="small" icon={<KeyOutlined />} onClick={() => openPerm(r)} />
          </Tooltip>
          {r.is_active ? (
            <Tooltip title="禁用账号">
              <Button type="text" size="small" danger icon={<StopOutlined />} onClick={() => handleToggleActive(r)} />
            </Tooltip>
          ) : (
            <Tooltip title="启用账号">
              <Button type="text" size="small" icon={<CheckCircleOutlined />} onClick={() => handleToggleActive(r)} />
            </Tooltip>
          )}
          <Tooltip title="彻底删除（不可恢复）">
            <Button type="text" size="small" danger icon={<DeleteOutlined />} onClick={() => openDestroy(r)} />
          </Tooltip>
        </Space>
      ),
    },
  ]

  return (
    <div className="user-admin-layout">
      <header className="user-admin-header">
      <div className="user-admin-header-left">
         <PlatformLogo size={32} />
         <span style={{ fontSize: 18, fontWeight: 600, color: 'var(--text-primary)' }}>用户管理</span>
       </div>
        <div className="user-admin-header-right">
          <Tooltip title={theme === 'light' ? '切换到暗色模式' : '切换到亮色模式'}>
            <Button type="text" icon={<BulbOutlined />} onClick={toggleTheme} style={{ borderRadius: 8 }} />
          </Tooltip>
          <Button icon={<ArrowLeftOutlined />} onClick={() => navigate('/admin')} style={{ borderRadius: 8 }}>
            返回管理后台
          </Button>
        </div>
      </header>

      <div className="user-admin-content">
        <div className="user-admin-stats">
          <div className="user-admin-stat-card stagger-item">
            <div className="user-admin-stat-icon" style={{ background: 'var(--color-primary-bg)', color: 'var(--color-primary)' }}>
              <UserOutlined />
            </div>
            <div>
              <div className="user-admin-stat-value">{users.length}</div>
              <div className="user-admin-stat-label">注册用户</div>
            </div>
          </div>
          <div className="user-admin-stat-card stagger-item">
            <div className="user-admin-stat-icon" style={{ background: 'rgba(22, 163, 74, 0.12)', color: '#16A34A' }}>
              <CheckCircleOutlined />
            </div>
            <div>
              <div className="user-admin-stat-value">{activeCount}</div>
              <div className="user-admin-stat-label">启用中</div>
            </div>
          </div>
          <div className="user-admin-stat-card stagger-item">
            <div className="user-admin-stat-icon" style={{ background: 'rgba(217, 119, 6, 0.12)', color: '#D97706' }}>
              <StopOutlined />
            </div>
            <div>
              <div className="user-admin-stat-value">{users.length - activeCount}</div>
              <div className="user-admin-stat-label">已禁用</div>
            </div>
          </div>
        </div>

        <div className="user-admin-section">
          <div className="user-admin-section-header">
         <div className="user-admin-section-title">
           <UserOutlined /> 用户列表
         </div>
         <Space>
           <Input
             placeholder="搜索用户名"
             prefix={<SearchOutlined style={{ color: 'var(--text-tertiary)' }} />}
             value={searchText}
             onChange={(e) => setSearchText(e.target.value)}
             allowClear
             style={{ width: 200, borderRadius: 8 }}
           />
           <Segmented
             value={roleFilter || 'all'}
             onChange={(v) => setRoleFilter(v === 'all' ? null : (v as string))}
             options={[
               { label: '全部', value: 'all' },
               { label: '观察者', value: 'viewer' },
               { label: '操作员', value: 'operator' },
               { label: '管理员', value: 'admin' },
             ]}
           />
            <Button
              type="default"
              icon={<PlusOutlined />}
              onClick={() => setCreateOpen(true)}
              style={{ background: 'var(--bg-primary)', borderColor: 'var(--border-color)', color: 'var(--text-primary)' }}
            >
              新建用户
            </Button>
         </Space>
          </div>
          <div className="user-admin-table">
            <Table
              columns={columns}
              dataSource={filteredUsers}
              rowKey="id"
              loading={loading}
              pagination={{ pageSize: 20, showSizeChanger: true, showTotal: (t) => `共 ${t} 个用户` }}
              rowClassName={(r: AdminUserInfo) => (!r.is_active ? 'row-disabled' : '')}
            />
          </div>
        </div>
      </div>

      {/* 编辑用户（邮箱/角色） */}
      <Modal
        className="user-admin-modal"
        title={`编辑用户 - ${editUser?.username || ''}`}
        open={!!editUser}
        onCancel={() => setEditUser(null)}
        onOk={() => editForm.submit()}
        confirmLoading={editSaving}
        okText="保存"
        cancelText="取消"
        destroyOnClose
      >
        <Form
          form={editForm}
          onFinish={handleEditSave}
          layout="vertical"
          preserve={false}
          initialValues={{ email: editUser?.email || '', role: editUser?.role || 'viewer' }}
        >
          <Form.Item name="email" label="邮箱">
            <Input placeholder="邮箱（可选）" />
          </Form.Item>
          <Form.Item name="role" label="角色" rules={[{ required: true, message: '请选择角色' }]}>
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
        </Form>
      </Modal>

      {/* Agent 授权 */}
      <Modal
        className="user-admin-modal"
        title={`Agent 授权 - ${permUser?.username || ''}`}
        open={!!permUser}
        onCancel={() => setPermUser(null)}
        onOk={handlePermSave}
        confirmLoading={permSaving}
        okText="保存"
        cancelText="取消"
      >
        <p style={{ marginBottom: 12, color: 'var(--text-secondary)' }}>勾选该用户可使用的 Agent：</p>
        <Checkbox.Group
          style={{ display: 'flex', flexDirection: 'column', gap: 8 }}
          value={permAgentIds}
          onChange={(vals) => setPermAgentIds(vals as number[])}
        >
          {agents.filter((a) => a.is_active).map((a) => (
            <Checkbox key={a.id} value={a.id}>
              {a.name}{a.description ? ` - ${a.description}` : ''}
            </Checkbox>
          ))}
        </Checkbox.Group>
      </Modal>

      {/* 新建用户：与登录页注册同字段同校验 */}
      <Modal
        className="user-admin-modal"
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

      <style>{`.row-disabled { opacity: 0.5; }`}</style>

      {/* 彻底删除两步确认 */}
      <Modal
        className="user-admin-modal"
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
              <Descriptions.Item label="用户"><strong style={{ color: '#ef4444' }}>{destroyTarget?.username}（{destroyTarget?.account}）</strong></Descriptions.Item>
              <Descriptions.Item label="管理员账号">{destroyUsername}</Descriptions.Item>
            </Descriptions>
            <p style={{ marginTop: 12, color: '#ef4444', fontWeight: 600 }}>点击"确认删除"后，数据将永久丢失！</p>
          </div>
        )}
      </Modal>
    </div>
  )
}
