import { useEffect, useRef, useState } from 'react'
import { Alert, Button, Checkbox, Form, Input, message, Modal } from 'antd'
import {
  LockOutlined, IdcardOutlined, MessageOutlined, AppstoreOutlined, DatabaseOutlined,
  ApiOutlined,
} from '@ant-design/icons'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { login, sendPasswordResetCode, resetPasswordByCode } from '../../api/auth'
import { useAuthStore } from '../../store/authStore'
import PlatformLogo from '../../components/PlatformLogo'
import { passwordPolicyError } from '../../utils/passwordPolicy'
import './index.css'

const FEATURES = [
  { icon: <MessageOutlined />, title: '对话问答', desc: '流式输出，推理过程全程可视' },
  { icon: <AppstoreOutlined />, title: '技能广场', desc: '技能包按角色授权，即装即用' },
  { icon: <DatabaseOutlined />, title: '知识库', desc: '内部文档检索与引用' },
  { icon: <ApiOutlined />, title: 'MCP 工具', desc: '外部能力即插即用' },
]

// 下线原因 -> 登录页提示文案。原因由 utils/forceLogout.ts 挂在 ?reason= 上：
// session_revoked = 管理员改动了角色/权限，session_expired = 登录态自然过期
const REASON_NOTICES: Record<string, string> = {
  session_revoked: '管理员已变更你的账号权限，请重新登录以生效',
  session_expired: '登录状态已过期，请重新登录',
  password_changed: '密码修改成功，请使用新密码登录',
}

// 仅记住账号；历史版本保存过的密码会在读取后立即清除。
const REMEMBER_KEY = 'tp-remember-login'
const encodeSaved = (obj: unknown) => btoa(unescape(encodeURIComponent(JSON.stringify(obj))))
const decodeSaved = (raw: string) => JSON.parse(decodeURIComponent(escape(atob(raw))))

export default function Login() {
  const [loading, setLoading] = useState(false)
  const [remember, setRemember] = useState(false)
  const [loginForm] = Form.useForm()
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const reasonNotice = REASON_NOTICES[searchParams.get('reason') || ''] || ''
  const setAuth = useAuthStore((state) => state.setAuth)

  // 记住密码：进入页面自动回填
  useEffect(() => {
    try {
      const raw = localStorage.getItem(REMEMBER_KEY)
      if (!raw) return
      const saved = decodeSaved(raw)
      if (saved?.account) {
        loginForm.setFieldsValue({ account: saved.account })
        localStorage.setItem(REMEMBER_KEY, encodeSaved({ account: saved.account }))
        setRemember(true)
      }
    } catch { /* 缓存损坏按无缓存处理 */ }
  }, [loginForm])

  // 登录（账号 + 密码）；自助注册已下线，账号由管理员在后台创建
  const onLogin = async (values: { account: string; password: string }) => {
    setLoading(true)
    try {
      const data = await login(values)
      if (remember) {
        localStorage.setItem(REMEMBER_KEY, encodeSaved({ account: values.account }))
      } else {
        localStorage.removeItem(REMEMBER_KEY)
      }
      setAuth(data.access_token, data.user)
      message.success('登录成功！')
      navigate(data.user.must_change_password ? '/force-password-change' : '/')
    } catch (error: any) {
      message.error(error.response?.data?.detail || '登录失败，请检查账号和密码')
    } finally {
      setLoading(false)
    }
  }

  // ===== 忘记密码（邮箱验证码两步重置）=====
  const [forgotOpen, setForgotOpen] = useState(false)
  const [forgotStep, setForgotStep] = useState<1 | 2>(1)
  const [forgotUser, setForgotUser] = useState('')
  const [forgotEmail, setForgotEmail] = useState('')
  const [resetCode, setResetCode] = useState('')
  const [newPwd, setNewPwd] = useState('')
  const [newPwd2, setNewPwd2] = useState('')
  const [sending, setSending] = useState(false)
  const [resetting, setResetting] = useState(false)
  const [countdown, setCountdown] = useState(0)
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null)

  useEffect(() => () => { if (timerRef.current) clearInterval(timerRef.current) }, [])

  const closeForgot = () => {
    setForgotOpen(false)
    setForgotStep(1)
    setForgotUser('')
    setForgotEmail('')
    setResetCode('')
    setNewPwd('')
    setNewPwd2('')
    setCountdown(0)
    if (timerRef.current) { clearInterval(timerRef.current); timerRef.current = null }
  }

  const startCountdown = () => {
    setCountdown(60)
    if (timerRef.current) clearInterval(timerRef.current)
    timerRef.current = setInterval(() => {
      setCountdown((s) => {
        if (s <= 1) {
          if (timerRef.current) { clearInterval(timerRef.current); timerRef.current = null }
          return 0
        }
        return s - 1
      })
    }, 1000)
  }

  const handleSendCode = async () => {
    const uname = forgotUser.trim()
    if (!uname) {
      message.warning('请输入用户名')
      return
    }
    const email = forgotEmail.trim()
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) {
      message.warning('请输入正确的邮箱地址')
      return
    }
    setSending(true)
    try {
      const res = await sendPasswordResetCode(uname, email)
      message.success(res?.message || '若该邮箱绑定了账号，验证码已发送')
      setForgotStep(2)
      startCountdown()
    } catch (e: any) {
      message.error(e.response?.data?.detail || '发送失败，请稍后再试')
    } finally {
      setSending(false)
    }
  }

  const handleReset = async () => {
    if (!/^\d{6}$/.test(resetCode.trim())) { message.warning('请输入 6 位验证码'); return }
    const policyError = passwordPolicyError(newPwd, forgotUser)
    if (policyError) { message.warning(policyError); return }
    if (newPwd !== newPwd2) { message.warning('两次输入的新密码不一致'); return }
    setResetting(true)
    try {
      const res = await resetPasswordByCode(forgotUser.trim(), forgotEmail.trim(), resetCode.trim(), newPwd)
      message.success(res?.message || '密码已重置，请登录')
      closeForgot()
    } catch (e: any) {
      message.error(e.response?.data?.detail || '重置失败')
    } finally {
      setResetting(false)
    }
  }

  return (
    <div className="login-layout">
      {/* Left: brand panel */}
      <aside className="login-brand">
        <div className="login-brand-content">
          <div className="login-brand-logo">
            <PlatformLogo size={64} tone="brand" />
          </div>
          <p className="login-brand-eyebrow">SparkWork · AI Agent Platform</p>
          <h1>一句提示，一步抵达。</h1>
          <p className="login-brand-desc">
            SparkWork 智能体工作台：对话问答、技能广场、知识库与运维监控，一站完成。
          </p>
          <div className="login-features">
            {FEATURES.map((f) => (
              <div key={f.title} className="login-feature-item">
                <div className="login-feature-icon">{f.icon}</div>
                <div className="login-feature-body">
                  <div className="login-feature-title">{f.title}</div>
                  <div className="login-feature-desc">{f.desc}</div>
                </div>
              </div>
            ))}
          </div>
          <div className="login-brand-meta">SparkWork · FastAPI · React</div>
        </div>
      </aside>

      {/* Right: form panel */}
      <main className="login-form-side">
        <div className="login-form-card">
          <div className="login-form-title">欢迎回来</div>
          <div className="login-form-subtitle">请输入账号和密码登录平台</div>
          {reasonNotice && (
            <Alert
              type="warning"
              showIcon
              message={reasonNotice}
              style={{ marginBottom: 16, borderRadius: 8 }}
            />
          )}
          <Form form={loginForm} name="login" onFinish={onLogin} autoComplete="off" size="large">
            <Form.Item name="account" rules={[{ required: true, message: '请输入账号' }]}>
              <Input prefix={<IdcardOutlined />} placeholder="账号" />
            </Form.Item>
            <Form.Item name="password" rules={[{ required: true, message: '请输入密码' }]}>
              <Input.Password prefix={<LockOutlined />} placeholder="密码" />
            </Form.Item>
            <div className="login-remember-row">
              <Checkbox checked={remember} onChange={(e) => setRemember(e.target.checked)}>记住账号</Checkbox>
              <a onClick={() => setForgotOpen(true)}>忘记密码？</a>
            </div>
            <Form.Item>
              <Button className="login-btn" type="primary" htmlType="submit" loading={loading} block>
                登录
              </Button>
            </Form.Item>
          </Form>
        </div>
        <div className="login-footer">TaskPilot · AI Agent Platform</div>
      </main>

      {/* 忘记密码弹窗：两步（发码 → 重置） */}
      <Modal
        title="忘记密码"
        open={forgotOpen}
        onCancel={closeForgot}
        footer={null}
        destroyOnClose
        width={420}
      >
        {forgotStep === 1 ? (
          <div className="forgot-step">
            <p className="forgot-tip">请输入用户名（登录账号）和绑定的邮箱，我们将向你发送重置密码的验证码。用户名或邮箱不符会提示错误；没有绑定邮箱的账号请联系管理员。</p>
            <Input
              size="large"
              placeholder="用户名（登录账号）"
              value={forgotUser}
              onChange={(e) => setForgotUser(e.target.value)}
            />
            <Input
              size="large"
              placeholder="账号绑定的邮箱"
              value={forgotEmail}
              onChange={(e) => setForgotEmail(e.target.value)}
              onPressEnter={handleSendCode}
            />
            <Button className="login-btn" type="primary" block loading={sending} onClick={handleSendCode}>
              发送验证码
            </Button>
          </div>
        ) : (
          <div className="forgot-step">
            <p className="forgot-tip">
              验证码已发送至 <b>{forgotEmail}</b>（5 分钟内有效）。
              {countdown > 0
                ? <span className="forgot-muted"> {countdown} 秒后可重发</span>
                : <a onClick={handleSendCode}> 重新发送</a>}
            </p>
            <Input
              size="large"
              placeholder="6 位验证码"
              maxLength={6}
              value={resetCode}
              onChange={(e) => setResetCode(e.target.value.replace(/\D/g, ''))}
            />
            <Input.Password
              size="large"
              placeholder="新密码（大写+小写+数字，至少 8 位）"
              value={newPwd}
              onChange={(e) => setNewPwd(e.target.value)}
            />
            <Input.Password
              size="large"
              placeholder="再次输入新密码"
              value={newPwd2}
              onChange={(e) => setNewPwd2(e.target.value)}
            />
            <Button className="login-btn" type="primary" block loading={resetting} onClick={handleReset}>
              重置密码
            </Button>
          </div>
        )}
      </Modal>
    </div>
  )
}
