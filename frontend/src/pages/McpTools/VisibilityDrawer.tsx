import { useEffect, useRef, useState } from 'react'
import { Alert, Button, Checkbox, Drawer, Select, Space, Switch, message } from 'antd'
import { EyeInvisibleOutlined, TeamOutlined, UserOutlined } from '@ant-design/icons'
import { getPeople, getPerson, type Person } from '../../api/people'
import { getMcpVisibility, saveMcpVisibility, type McpServerInfo, type McpVisibility } from '../../api/mcp'
import { readableError } from '../../api/governance'
import { ROLE_LABELS } from '../../constants/roles'

export default function VisibilityDrawer({ server, onClose, onSaved }: {
  server: McpServerInfo | null; onClose: () => void; onSaved: () => Promise<void>
}) {
  const [data, setData] = useState<McpVisibility | null>(null)
  const [people, setPeople] = useState<Person[]>([])
  const [selected, setSelected] = useState<Person[]>([])
  const [search, setSearch] = useState('')
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const generation = useRef(0)
  useEffect(() => {
    let active = true
    setData(null); setError(''); setSelected([])
    if (server) getMcpVisibility(server.id).then(async (result) => {
      if (!active) return
      setData(result)
      const users = await Promise.all(result.user_ids.map((id) => getPerson(id).catch(() => null)))
      if (active) setSelected(users.filter((person): person is Person => !!person))
    }).catch((cause) => { if (active) setError(readableError(cause)) })
    return () => { active = false }
  }, [server?.id])
  useEffect(() => {
    if (!server) return
    const current = ++generation.current
    const timer = window.setTimeout(() => {
      setLoading(true)
      getPeople({ page: 1, page_size: 50, search, status: 'active' }).then((result) => {
        if (current === generation.current) setPeople(result.items)
      }).catch((cause) => { if (current === generation.current) setError(readableError(cause)) })
        .finally(() => { if (current === generation.current) setLoading(false) })
    }, 200)
    return () => { window.clearTimeout(timer); generation.current++ }
  }, [search, server?.id])
  async function save() {
    if (!server || !data) return
    setSaving(true); setError('')
    try {
      await saveMcpVisibility(server.id, data)
      await onSaved(); message.success('MCP 可见范围已保存'); onClose()
    } catch (cause) { setError(readableError(cause)) } finally { setSaving(false) }
  }
  const options = [...people, ...selected.filter((person) => !people.some((item) => item.id === person.id))]
  return <Drawer className="mcp-visibility-drawer" title={`可见范围 · ${server?.name || ''}`} open={!!server}
    width="min(480px, 94vw)" onClose={onClose} footer={<Space className="mcp-drawer-footer">
      <Button onClick={onClose}>取消</Button><Button type="primary" loading={saving} disabled={!data} onClick={() => void save()}>保存可见范围</Button>
    </Space>}>
    {error && <Alert type="error" showIcon message={error} />}
    <section className="mcp-visibility-intro"><EyeInvisibleOutlined /><div><h3>限定可见</h3>
      <p>仅向所选角色或人员开放，问答中的选择与调用同步受限。</p></div>
      <Switch aria-label="限定可见" checked={data?.hidden} disabled={!data} onChange={(hidden) => data && setData({ ...data, hidden })} /></section>
    {data?.hidden ? <>
      <section className="mcp-visibility-section"><h4><TeamOutlined /> 按角色开放</h4>
        <Checkbox.Group aria-label="可见角色" value={data.roles} options={['viewer', 'operator', 'admin'].map((role) => ({ label: ROLE_LABELS[role], value: role }))}
          onChange={(roles) => setData({ ...data, roles: roles.map(String) })} /></section>
      <section className="mcp-visibility-section"><h4><UserOutlined /> 单独向人员开放</h4>
        <p>无需改变人员角色，搜索姓名或账号添加。</p>
        <Select aria-label="可见人员" mode="multiple" showSearch filterOption={false} loading={loading} value={data.user_ids}
          placeholder="搜索姓名或账号" onSearch={setSearch} optionFilterProp="label"
          options={options.map((person) => ({ value: person.id, label: `${person.username}（${person.account || '无账号'}）` }))}
          onChange={(user_ids) => { setData({ ...data, user_ids }); setSelected(options.filter((person) => user_ids.includes(person.id))) }} />
      </section>
      {!data.roles.length && !data.user_ids.length && <Alert type="warning" showIcon message="未选择任何对象：保存后全员不可见，管理员仍可在后台管理。" />}
    </> : <Alert type="info" showIcon message="全部角色可见且可用；停用或删除的 MCP 仍不可使用。" />}
  </Drawer>
}
