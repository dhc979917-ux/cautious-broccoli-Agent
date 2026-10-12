import { useEffect, useState } from 'react'
import { Alert, Button, Select, Space, Tag, message } from 'antd'
import { getModelExtraGrants, saveModelExtraGrants, type ModelExtraGrants as GrantData } from '../../api/model'
import { invalidateModelDirectory } from '../../store/modelDirectory'

export default function ModelExtraGrants({ userId, onSaved }: { userId: number; onSaved: () => Promise<void> }) {
  const [data, setData] = useState<GrantData | null>(null)
  const [models, setModels] = useState<string[]>([])
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)
  const [refresh, setRefresh] = useState(0)
  useEffect(() => {
    let alive = true
    setData(null); setError('')
    getModelExtraGrants(userId).then((next) => {
      if (alive) { setData(next); setModels(next.models) }
    }).catch(() => { if (alive) setError('模型授权加载失败，请重试。') })
    return () => { alive = false }
  }, [userId, refresh])
  async function save() {
    if (!data) return
    setSaving(true)
    try {
      await saveModelExtraGrants(userId, models, data.revision)
      invalidateModelDirectory()
      await onSaved()
      setRefresh((value) => value + 1)
      message.success('额外模型授权已保存')
    } catch (cause: any) {
      message.error(cause.response?.status === 409 ? '人员授权已更新，请刷新后重试。' : '保存模型授权失败')
      setRefresh((value) => value + 1)
    } finally { setSaving(false) }
  }
  return <div className="people-model-grants" style={{ marginBottom: 24 }}>
    <div className="ant-form-item-label"><label>除权限外模型授权</label></div>
    {error ? <Alert type="error" showIcon message={error} action={<Button size="small" onClick={() => setRefresh((value) => value + 1)}>重试</Button>} /> : <>
      <Select mode="multiple" style={{ width: '100%' }} showSearch optionFilterProp="label" loading={!data}
        disabled={!data || saving} value={models} onChange={setModels} placeholder="选择角色权限之外的模型"
        options={data?.catalog.filter((modelId) => !data.role_models.includes(modelId)).map((modelId) => ({
          value: modelId, label: `${data.display_names[modelId] || modelId}${data.disabled.includes(modelId) ? '（停用）' : ''}`,
        }))} />
      <Space wrap style={{ marginTop: 8 }}>{models.filter((modelId) => data?.disabled.includes(modelId)).map((modelId) =>
        <Tag key={modelId}>停用授权保留，启用后可用</Tag>)}
        <Button size="small" loading={saving} disabled={!data} onClick={() => void save()}>保存额外模型授权</Button>
      </Space>
    </>}
  </div>
}
