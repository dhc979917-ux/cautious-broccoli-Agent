import { useState } from 'react'
import { Alert, Button, Card, Form, Input, Typography, message } from 'antd'
import { LockOutlined, SafetyCertificateOutlined } from '@ant-design/icons'
import { changeMyPassword } from '../../api/profile'
import { useAuthStore } from '../../store/authStore'
import { passwordPolicyError } from '../../utils/passwordPolicy'
import PlatformLogo from '../../components/PlatformLogo'
import './index.css'

const { Title, Paragraph } = Typography

export default function ForcePasswordChange() {
  const [loading, setLoading] = useState(false)
  const user = useAuthStore((state) => state.user)
  const logout = useAuthStore((state) => state.logout)

  const submit = async (values: { old_password: string; new_password: string; confirm: string }) => {
    const error = passwordPolicyError(values.new_password, user?.account)
    if (error) { message.error(error); return }
    if (values.new_password !== values.confirm) { message.error('两次输入的新密码不一致'); return }
    setLoading(true)
    try {
      await changeMyPassword(values.old_password, values.new_password)
      logout()
      message.success('密码修改成功，请使用新密码重新登录')
      window.location.replace('/login?reason=password_changed')
    } catch (e: any) {
      message.error(e.response?.data?.detail || '密码修改失败')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="force-password-page">
      <Card className="force-password-card" bordered={false}>
        <div className="force-password-logo"><PlatformLogo width={160} /></div>
        <SafetyCertificateOutlined className="force-password-icon" />
        <Title level={2}>首次登录，请修改密码</Title>
        <Paragraph type="secondary">为保障账号安全，完成密码修改前无法访问其他功能。</Paragraph>
        <Alert type="info" showIcon message="新密码至少 8 位，且必须同时包含大写字母、小写字母和数字。" />
        <Form layout="vertical" onFinish={submit} autoComplete="off" requiredMark={false}>
          <Form.Item name="old_password" label="当前初始密码" rules={[{ required: true, message: '请输入当前密码' }]}>
            <Input.Password prefix={<LockOutlined />} autoComplete="current-password" />
          </Form.Item>
          <Form.Item name="new_password" label="新密码" rules={[{ required: true, message: '请输入新密码' }]}>
            <Input.Password prefix={<LockOutlined />} autoComplete="new-password" />
          </Form.Item>
          <Form.Item name="confirm" label="确认新密码" rules={[{ required: true, message: '请再次输入新密码' }]}>
            <Input.Password prefix={<LockOutlined />} autoComplete="new-password" />
          </Form.Item>
          <Button type="primary" htmlType="submit" block size="large" loading={loading}>修改密码并重新登录</Button>
        </Form>
      </Card>
    </div>
  )
}
