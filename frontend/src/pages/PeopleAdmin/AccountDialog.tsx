import { useEffect, useState } from 'react'
import { Alert, Form, Input, Modal, message } from 'antd'
import { registerUser } from '../../api/auth'
import { hardDeleteUser, resetUserPassword, updateUser } from '../../api/admin'
import { readableError } from '../../api/governance'
import { useAuthStore } from '../../store/authStore'
import type { Person } from '../../api/people'
import { INITIAL_PASSWORD } from '../../utils/passwordPolicy'

export type AccountAction = 'create' | 'edit' | 'reset' | 'delete'

export default function AccountDialog({ action, person, onClose, onComplete }: {
  action: AccountAction | null; person: Person | null; onClose: () => void; onComplete: (deleted: boolean) => Promise<void>
}) {
  const user = useAuthStore((state) => state.user)
  const [form] = Form.useForm()
  const [saving, setSaving] = useState(false)
  useEffect(() => {
    if (!action) return
    form.resetFields()
    if (action === 'edit' && person) form.setFieldsValue({ username: person.username, account: person.account, email: person.email })
    if (action === 'reset' || action === 'delete') form.setFieldsValue({ admin_account: user?.account || '' })
  }, [action, person?.id])
  async function save(values: Record<string, string>) {
    if (user?.role !== 'admin') return
    setSaving(true)
    try {
      if (action === 'create') await registerUser({ account: values.account, username: values.username, phone: values.phone, email: values.email || null, password: INITIAL_PASSWORD })
      else if (person && action === 'edit') await updateUser(person.id, { username: values.username, account: values.account, email: values.email })
      else if (person && action === 'reset') await resetUserPassword(person.id, values.admin_account, values.admin_password)
      else if (person && action === 'delete') {
        if (values.confirm_account !== person.account) { message.error('请输入目标人员的账号以确认删除。'); return }
        await hardDeleteUser(person.id, values.admin_account, values.admin_password)
      }
      await onComplete(action === 'delete')
      onClose()
      message.success(action === 'reset' ? '密码已重置，下次登录必须修改初始密码。' : '操作已完成')
    } catch (cause) { message.error(readableError(cause)) } finally { setSaving(false) }
  }
  const titles = { create: '新增人员', edit: '编辑账号资料', reset: '重置登录密码', delete: '删除人员' }
  return <Modal title={action ? titles[action] : ''} open={!!action} onCancel={onClose} onOk={() => form.submit()}
    confirmLoading={saving} okText={action === 'delete' ? '确认删除' : '确认'} cancelText="取消" destroyOnClose>
    <Form form={form} layout="vertical" onFinish={(values) => void save(values)} preserve={false}>
      {(action === 'create' || action === 'edit') && <>
        <Form.Item name="username" label="姓名" rules={[{ required: true, message: '请输入姓名' }]}><Input /></Form.Item>
        <Form.Item name="account" label="账号" rules={[{ required: true, message: '请输入账号' }]}><Input /></Form.Item>
        {action === 'create' && <Form.Item name="phone" label="手机号" rules={[{ required: true }, { pattern: /^\d{11}$/, message: '请输入11位手机号' }]}><Input maxLength={11} /></Form.Item>}
        <Form.Item name="email" label="邮箱" rules={[{ type: 'email', message: '邮箱格式不正确' }]}><Input /></Form.Item>
        {action === 'create' && <Alert showIcon type="info" message="新人员使用平台统一初始密码，首次登录必须修改。" />}
      </>}
      {(action === 'reset' || action === 'delete') && <>
        <Alert showIcon type="warning" message={action === 'delete' ? '删除将移除该人员及关联数据，操作不可恢复。' : '重置后旧登录会话立即失效，需使用初始密码重新登录。'} style={{ marginBottom: 20 }} />
        {action === 'delete' && <Form.Item name="confirm_account" label={`输入人员账号 ${person?.account} 确认`} rules={[{ required: true }]}><Input autoComplete="off" /></Form.Item>}
        <Form.Item name="admin_account" label="管理员账号" rules={[{ required: true }]}><Input autoComplete="off" /></Form.Item>
        <Form.Item name="admin_password" label="管理员密码" rules={[{ required: true }]}><Input.Password autoComplete="new-password" /></Form.Item>
      </>}
    </Form>
  </Modal>
}
