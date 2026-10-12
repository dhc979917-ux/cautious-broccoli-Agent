import { useEffect, useRef, useState } from 'react'
import { Alert, Button, Select, Space, Switch, Tag, Typography, message } from 'antd'
import { getPeople, type Person } from '../../api/people'
import { changeModelAuthorization, getModelAuthorization, type ModelAuthorization as AuthorizationData } from '../../api/model'
import { ROLE_LABELS } from '../../constants/roles'
import { invalidateModelDirectory } from '../../store/modelDirectory'

const roles = ['viewer', 'operator', 'admin']

export default function ModelAuthorization({ modelId, onChanged }: { modelId: string; onChanged: () => Promise<void> }) {
  const [data, setData] = useState<AuthorizationData | null>(null)
  const [scope, setScope] = useState<'role' | 'user'>('role')
  const [role, setRole] = useState('viewer')
  const [userId, setUserId] = useState<number>()
  const [users, setUsers] = useState<Person[]>([])
  const [selectedUser, setSelectedUser] = useState<Person>()
  const [search, setSearch] = useState('')
  const [userLoading, setUserLoading] = useState(false)
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)
  const [refresh, setRefresh] = useState(0)
  const searchSequence = useRef(0)
  useEffect(() => {
    let alive = true
    setData(null); setError('')
    getModelAuthorization(modelId, userId).then((next) => { if (alive) setData(next) })
      .catch(() => { if (alive) setError('授权信息加载失败，请重试。') })
    return () => { alive = false }
  }, [modelId, userId, refresh])
  useEffect(() => {
    const sequence = ++searchSequence.current
    const timer = window.setTimeout(() => {
      setUserLoading(true)
      getPeople({ page: 1, page_size: 30, search, sort_by: 'username', sort_order: 'asc' }).then((result) => {
        if (sequence === searchSequence.current) setUsers(result.items)
      }).catch(() => { if (sequence === searchSequence.current) message.error('用户搜索失败，请重试') })
        .finally(() => { if (sequence === searchSequence.current) setUserLoading(false) })
    }, 250)
    return () => { window.clearTimeout(timer); searchSequence.current += 1 }
  }, [search])
  const selectedRole = data?.roles.find((item) => item.role === role)
  const permitted = scope === 'role' ? !!selectedRole?.allowed : !!data?.user?.extra_allowed
  const inherited = scope === 'user' && !!data?.user?.role_allowed
  async function change(allowed: boolean | null) {
    if (!data || (scope === 'user' && !userId)) return
    setSaving(true)
    try {
      await changeModelAuthorization(modelId, scope, scope === 'role' ? role : String(userId), allowed)
      invalidateModelDirectory()
      setRefresh((value) => value + 1)
      await onChanged()
      message.success(allowed === null ? '已恢复该模型的默认授权' : '模型授权已更新')
    } catch { message.error('更新模型授权失败，请重试') } finally { setSaving(false) }
  }
  return <div className="model-access-layout">
    {error && <Alert type="error" showIcon message={error} action={<Button onClick={() => setRefresh((value) => value + 1)}>重试</Button>} />}
    <section className="model-access-section">
      <Typography.Title level={5}>按角色授权</Typography.Title>
      <div className="model-access-row">
        <Select aria-label="授权角色" value={role} onChange={(value) => { setRole(value); setScope('role') }}
          options={roles.map((value) => ({ value, label: ROLE_LABELS[value] }))} />
        <Space><Typography.Text>允许使用</Typography.Text><Switch checked={!!selectedRole?.allowed}
          loading={saving} disabled={!data} onChange={(value) => { setScope('role'); void changeRole(value) }} /></Space>
      </div>
      <Space wrap>{data?.roles.map((item) => <Tag key={item.role} color={item.allowed ? 'blue' : 'default'}>
        {ROLE_LABELS[item.role]}：{item.allowed ? '允许' : '未授权'}</Tag>)}</Space>
    </section>
    <section className="model-access-section">
      <Typography.Title level={5}>按用户额外授权</Typography.Title>
      <Select aria-label="授权用户" showSearch filterOption={false} allowClear loading={userLoading}
        value={userId} onSearch={setSearch} onFocus={() => setScope('user')}
        onChange={(value) => { setUserId(value); setSelectedUser(users.find((person) => person.id === value) || selectedUser); setScope('user') }} placeholder="搜索用户姓名或账号"
        options={[...users, ...(selectedUser && !users.some((person) => person.id === selectedUser.id) ? [selectedUser] : [])]
          .map((person) => ({ value: person.id, label: `${person.username}（${person.account || '无账号'}）` }))} />
      {userId && <div className="model-user-status"><Tag color={data?.user?.allowed ? 'blue' : 'default'}>
        {!data ? '加载中' : data.user?.role_allowed ? '角色已授权' : data.user?.extra_allowed ? '额外已授权' : '未授权'}</Tag>
        {inherited && <Typography.Text type="secondary">该用户已通过角色获得权限，无需额外授权。</Typography.Text>}</div>}
    </section>
    <div className="model-access-footer">
      <Select aria-label="本次操作对象" value={scope} onChange={setScope}
        options={[{ value: 'role', label: '当前角色' }, { value: 'user', label: '当前用户' }]} />
      <Space wrap><Button type="primary" loading={saving} disabled={!data || (scope === 'user' && (!userId || inherited || !data.is_chat))}
        onClick={() => void change(!permitted)}>{permitted ? '移除该模型' : '授予该模型'}</Button>
        <Button loading={saving} disabled={!data || (scope === 'role' ? !selectedRole?.overridden : !userId || !data.user?.extra_allowed)}
          onClick={() => void change(null)}>恢复默认授权</Button></Space>
    </div>
  </div>

  async function changeRole(allowed: boolean) {
    setSaving(true)
    try {
      await changeModelAuthorization(modelId, 'role', role, allowed)
      invalidateModelDirectory(); setRefresh((value) => value + 1)
      await onChanged()
      message.success('角色模型授权已更新')
    } catch { message.error('更新角色授权失败，请重试') } finally { setSaving(false) }
  }
}
