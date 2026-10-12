import { useEffect, useRef, useState } from 'react'
import { Avatar, Button, Card, Form, Input, Modal, Progress, Slider, Space, Statistic, Tag, Tooltip, message } from 'antd'
import { UserOutlined, MailOutlined, PhoneOutlined, EditOutlined, CameraOutlined, RobotOutlined, EyeOutlined, EyeInvisibleOutlined } from '@ant-design/icons'
import Cropper from 'react-easy-crop'
import type { Area } from 'react-easy-crop'
import { getMyProfile, updateMyProfile, changeMyPassword, getMyUsage, uploadMyAvatar, uploadMyAgentAvatar, getMyMailPassword, setMyMailboxPassword, MyProfile, MyUsage } from '../../api/profile'
import { useAuthStore } from '../../store/authStore'
import { ROLE_COLORS, ROLE_LABELS } from '../../constants/roles'
import { passwordPolicyError } from '../../utils/passwordPolicy'
import './index.css'

function fmtTime(s?: string | null): string {
  if (!s) return '—'
  return new Date(s).toLocaleString('zh-CN', { hour12: false })
}

function fmtBytes(b: number): string {
  if (!b) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB']
  let v = b, i = 0
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++ }
  return `${v >= 100 ? Math.round(v) : v.toFixed(1)} ${units[i]}`
}

// ===== 头像裁剪工具：加载图片 → 按裁剪区域绘制到 256×256 canvas → 输出 PNG =====
function loadImage(src: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const img = new Image()
    img.onload = () => resolve(img)
    img.onerror = reject
    img.src = src
  })
}

async function getCroppedPng(imageSrc: string, pixelCrop: Area): Promise<Blob> {
  const image = await loadImage(imageSrc)
  const canvas = document.createElement('canvas')
  canvas.width = 256
  canvas.height = 256
  const ctx = canvas.getContext('2d')
  if (!ctx) throw new Error('canvas 不可用')
  ctx.drawImage(image, pixelCrop.x, pixelCrop.y, pixelCrop.width, pixelCrop.height, 0, 0, 256, 256)
  return new Promise((resolve, reject) => {
    canvas.toBlob((blob) => (blob ? resolve(blob) : reject(new Error('裁剪失败'))), 'image/png')
  })
}

export default function Profile() {
  const [profile, setProfile] = useState<MyProfile | null>(null)
  const [usage, setUsage] = useState<MyUsage | null>(null)
  const [pwdForm] = Form.useForm()
  const [savingPwd, setSavingPwd] = useState(false)
  // 行内编辑：邮箱 / 手机号（复用原保存逻辑）
  const [editingField, setEditingField] = useState<null | 'email' | 'phone'>(null)
  const [editingValue, setEditingValue] = useState('')
  const [savingContact, setSavingContact] = useState(false)
  // 邮箱密码（邮件 MCP 凭据）：平时黑点占位，眼睛按钮按需拉明文（刷新复原），可编辑保存
  const [mailPwdRevealed, setMailPwdRevealed] = useState(false)
  const [mailPwdValue, setMailPwdValue] = useState('')
  const [mailPwdEditing, setMailPwdEditing] = useState(false)
  const [mailPwdDraft, setMailPwdDraft] = useState('')
  const [savingMailPwd, setSavingMailPwd] = useState(false)
  // 修改密码：点击进入表单
  const [pwdMode, setPwdMode] = useState(false)
  // 头像上传与裁剪
  const updateUser = useAuthStore((s) => s.updateUser)
  // Agent 头像已全局化：仅管理员能在个人信息页设置（写全局），其他人不再显示该入口
  const isAdmin = useAuthStore((s) => s.user?.role === 'admin')
  const avatarInputRef = useRef<HTMLInputElement>(null)
  const agentInputRef = useRef<HTMLInputElement>(null)
  const [cropModalOpen, setCropModalOpen] = useState(false)
  const [cropKind, setCropKind] = useState<'user' | 'agent'>('user')
  const [cropSrc, setCropSrc] = useState('')
  const [crop, setCrop] = useState({ x: 0, y: 0 })
  const [zoom, setZoom] = useState(1)
  const [croppedAreaPixels, setCroppedAreaPixels] = useState<Area | null>(null)
  const [uploadingAvatar, setUploadingAvatar] = useState(false)

  const load = async () => {
    try {
      const [p, u] = await Promise.all([getMyProfile(), getMyUsage()])
      setProfile(p)
      setUsage(u)
    } catch {
      message.error('加载个人信息失败')
    }
  }

  useEffect(() => { load() }, [])

  const onAvatarFileChange = (kind: 'user' | 'agent') => (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    e.target.value = ''
    if (!file) return
    setCropKind(kind)
    if (!file.type.startsWith('image/')) {
      message.error('请选择图片文件')
      return
    }
    if (file.size > 2 * 1024 * 1024) {
      message.error('图片不能超过 2MB')
      return
    }
    setCropSrc(URL.createObjectURL(file))
    setCrop({ x: 0, y: 0 })
    setZoom(1)
    setCroppedAreaPixels(null)
    setCropModalOpen(true)
  }

  const onCropConfirm = async () => {
    if (!cropSrc || !croppedAreaPixels) return
    setUploadingAvatar(true)
    try {
      const blob = await getCroppedPng(cropSrc, croppedAreaPixels)
      const isAgent = cropKind === 'agent'
      const file = new File([blob], isAgent ? 'agent-avatar.png' : 'avatar.png', { type: 'image/png' })
      if (isAgent) {
        const data = await uploadMyAgentAvatar(file)
        message.success('Agent 头像已更新')
        updateUser({ agent_avatar_url: data.agent_avatar_url })
      } else {
        const data = await uploadMyAvatar(file)
        message.success('头像已更新')
        updateUser({ avatar_url: data.avatar_url })
      }
      setCropModalOpen(false)
      load()
    } catch (e: any) {
      message.error(e.response?.data?.detail || '头像上传失败')
    } finally {
      setUploadingAvatar(false)
    }
  }

  const startEdit = (field: 'email' | 'phone') => {
    setEditingField(field)
    setEditingValue(field === 'email' ? (profile?.email || '') : (profile?.phone || ''))
  }

  // 点眼睛：按需从后端拉明文显示；再点隐藏并清掉本地明文（刷新页面也会自动复原）
  const revealMailPwd = async () => {
    try {
      const data = await getMyMailPassword()
      setMailPwdValue(data.mail_password || '')
      setMailPwdRevealed(true)
    } catch (e: any) {
      message.error(e.response?.data?.detail || '获取邮箱密码失败')
    }
  }

  const hideMailPwd = () => {
    setMailPwdRevealed(false)
    setMailPwdValue('')
  }

  // 保存邮箱密码：只写入本平台存储（不会修改邮箱平台的密码）
  const saveMailPwd = async () => {
    const v = mailPwdDraft.trim()
    if (!v) {
      message.warning('请输入邮箱密码')
      return
    }
    setSavingMailPwd(true)
    try {
      await setMyMailboxPassword(v)
      message.success('邮箱密码已保存')
      setMailPwdEditing(false)
      setMailPwdDraft('')
      hideMailPwd()
      load()
    } catch (e: any) {
      message.error(e.response?.data?.detail || '保存失败')
    } finally {
      setSavingMailPwd(false)
    }
  }

  const saveContact = async () => {
    if (!editingField) return
    const v = editingValue.trim()
    if (editingField === 'email' && v && !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(v)) {
      message.error('邮箱格式不正确')
      return
    }
    if (editingField === 'phone' && !/^\d{11}$/.test(v)) {
      message.error('手机号必须为11位数字')
      return
    }
    setSavingContact(true)
    try {
      await updateMyProfile(editingField === 'email' ? { email: v } : { phone: v })
      message.success(editingField === 'email' ? '邮箱已更新' : '手机号已更新')
      setEditingField(null)
      load()
    } catch (e: any) {
      message.error(e.response?.data?.detail || '更新失败')
    } finally {
      setSavingContact(false)
    }
  }

  const savePwd = async (values: { old_password: string; new_password: string; confirm: string }) => {
    const policyError = passwordPolicyError(values.new_password, profile?.account)
    if (policyError) {
      message.error(policyError)
      return
    }
    if (values.new_password !== values.confirm) {
      message.error('两次输入的新密码不一致')
      return
    }
    setSavingPwd(true)
    try {
      await changeMyPassword(values.old_password, values.new_password)
      useAuthStore.getState().logout()
      message.success('密码修改成功，请重新登录')
      window.location.replace('/login?reason=password_changed')
    } catch (e: any) {
      message.error(e.response?.data?.detail || '修改失败')
    } finally {
      setSavingPwd(false)
    }
  }

  if (!profile) return <div className="profile-page"><div className="profile-loading">加载中…</div></div>

  const quotaPct = usage && usage.files_quota_bytes
    ? Math.min(100, Math.round((usage.files_bytes / usage.files_quota_bytes) * 100))
    : 0

  return (
    <div className="profile-page">
      <div className="profile-header">
        <div className="profile-avatar-col">
          <div className="profile-avatar-wrap" onClick={() => avatarInputRef.current?.click()}>
            <Avatar size={64} src={profile.avatar_url || undefined} icon={<UserOutlined />} className="profile-avatar" />
            <div className="profile-avatar-mask">
              <CameraOutlined />
              <span>更换头像</span>
            </div>
          </div>
          <span className="profile-avatar-caption">我的头像</span>
        </div>
        <input ref={avatarInputRef} type="file" accept="image/*" style={{ display: 'none' }} onChange={onAvatarFileChange('user')} />
        <div className="profile-title">
          <div className="profile-name">
            {profile.username}
            <Tag color={ROLE_COLORS[profile.role] || 'default'} style={{ marginLeft: 10 }}>
              {ROLE_LABELS[profile.role] || profile.role}
            </Tag>
          </div>
          <div className="profile-sub">注册于 {fmtTime(profile.created_at)} · 最近登录 {fmtTime(profile.last_login)}</div>
        </div>
        {isAdmin && (
          <>
            <div className="profile-avatar-col agent">
              <div className="profile-avatar-wrap" onClick={() => agentInputRef.current?.click()}>
                <Avatar size={64} src={profile.agent_avatar_url || undefined} icon={<RobotOutlined />} className="profile-avatar" />
                <div className="profile-avatar-mask">
                  <CameraOutlined />
                  <span>更换头像</span>
                </div>
              </div>
              <span className="profile-avatar-caption">Agent 头像（全局）</span>
            </div>
            <input ref={agentInputRef} type="file" accept="image/*" style={{ display: 'none' }} onChange={onAvatarFileChange('agent')} />
          </>
        )}
      </div>

      {/* 头像裁剪弹窗 */}
      <Modal
        title="裁剪头像"
        open={cropModalOpen}
        onCancel={() => setCropModalOpen(false)}
        onOk={onCropConfirm}
        okText="确定"
        cancelText="取消"
        confirmLoading={uploadingAvatar}
        destroyOnClose
      >
        <div style={{ position: 'relative', height: 320, background: '#000', borderRadius: 8, overflow: 'hidden' }}>
          {cropSrc && (
            <Cropper
              image={cropSrc}
              crop={crop}
              zoom={zoom}
              aspect={1}
              cropShape="round"
              showGrid={false}
              onCropChange={setCrop}
              onZoomChange={setZoom}
              onCropComplete={(_area, areaPixels) => setCroppedAreaPixels(areaPixels)}
            />
          )}
        </div>
        <div style={{ marginTop: 14, display: 'flex', alignItems: 'center', gap: 10 }}>
          <span style={{ fontSize: 13, color: 'var(--text-secondary)' }}>缩放</span>
          <Slider min={1} max={3} step={0.05} value={zoom} onChange={setZoom} style={{ flex: 1 }} />
        </div>
      </Modal>

      <div className="profile-grid">
        <Card title={<span><UserOutlined /> 账号信息</span>} className="profile-card">
          <div style={{ marginBottom: 20 }}>
            <div style={{ fontSize: 13, color: 'var(--text-secondary)', marginBottom: 6 }}>用户名</div>
            <div style={{ color: 'var(--text-primary)', fontWeight: 500 }}>{profile.username}</div>
          </div>
          <div style={{ marginBottom: 20 }}>
            <div style={{ fontSize: 13, color: 'var(--text-secondary)', marginBottom: 6 }}>账号</div>
            <div style={{ color: 'var(--text-primary)', fontWeight: 500 }}>{profile.account || '—'}</div>
          </div>
          <div>
            <div style={{ fontSize: 13, color: 'var(--text-secondary)', marginBottom: 6 }}>修改密码</div>
            {!pwdMode ? (
              <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <span style={{ color: 'var(--text-tertiary)' }}>定期修改密码，保障账号安全</span>
                <Tooltip title="修改密码"><EditOutlined style={{ color: 'var(--text-tertiary)', cursor: 'pointer' }} onClick={() => setPwdMode(true)} /></Tooltip>
              </div>
            ) : (
              <Form form={pwdForm} layout="vertical" onFinish={savePwd} style={{ marginTop: 8 }}>
                <Form.Item name="old_password" label="当前密码" rules={[{ required: true, message: '请输入当前密码' }]}>
                  <Input.Password />
                </Form.Item>
                <Form.Item name="new_password" label="新密码" rules={[{ required: true, message: '请输入新密码' }]}>
                  <Input.Password />
                </Form.Item>
                <Form.Item name="confirm" label="确认新密码" rules={[{ required: true, message: '请再次输入新密码' }]}>
                  <Input.Password />
                </Form.Item>
                <Space>
                  <Button type="primary" htmlType="submit" loading={savingPwd}>确定</Button>
                  <Button onClick={() => { setPwdMode(false); pwdForm.resetFields() }}>取消</Button>
                </Space>
              </Form>
            )}
          </div>
        </Card>
        <Card title={<span><MailOutlined /> 联系方式</span>} className="profile-card">
          <div style={{ marginBottom: 20 }}>
            <div style={{ fontSize: 13, color: 'var(--text-secondary)', marginBottom: 6 }}>邮箱地址</div>
            {editingField === 'email' ? (
              <Space.Compact style={{ width: '100%' }}>
                <Input value={editingValue} onChange={(e) => setEditingValue(e.target.value)} placeholder="选填" onPressEnter={saveContact} />
                <Button type="primary" loading={savingContact} onClick={saveContact}>确定</Button>
              </Space.Compact>
            ) : (
              <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <span style={{ color: 'var(--text-primary)' }}>{profile.email || '—'}</span>
                <Tooltip title="修改邮箱"><EditOutlined style={{ color: 'var(--text-tertiary)', cursor: 'pointer' }} onClick={() => startEdit('email')} /></Tooltip>
              </div>
            )}
          </div>
          {/* 邮箱密码（邮件助手凭据）：黑点占位 + 眼睛查看 + 编辑保存 */}
          <div style={{ marginBottom: 20 }}>
            <div style={{ fontSize: 13, color: 'var(--text-secondary)', marginBottom: 6 }}>
              邮箱密码
              <span style={{ marginLeft: 8, fontSize: 12, color: 'var(--text-tertiary)' }}>
                用于邮件助手收发信，仅保存在本平台
              </span>
            </div>
            {mailPwdEditing ? (
              <Space.Compact style={{ width: '100%' }}>
                <Input.Password
                  value={mailPwdDraft}
                  onChange={(e) => setMailPwdDraft(e.target.value)}
                  placeholder="邮箱平台的独立密码/授权码"
                  onPressEnter={saveMailPwd}
                />
                <Button type="primary" loading={savingMailPwd} onClick={saveMailPwd}>确定</Button>
                <Button onClick={() => { setMailPwdEditing(false); setMailPwdDraft('') }}>取消</Button>
              </Space.Compact>
            ) : (
              <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                {profile.has_mail_password ? (
                  mailPwdRevealed ? (
                    <span style={{ color: 'var(--text-primary)' }}>{mailPwdValue || '••••••'}</span>
                  ) : (
                    <span style={{ color: 'var(--text-primary)', letterSpacing: 2 }}>••••••••</span>
                  )
                ) : (
                  <span style={{ color: 'var(--text-tertiary)' }}>未设置</span>
                )}
                <Tooltip title={mailPwdRevealed ? '隐藏密码' : '查看密码'}>
                  {mailPwdRevealed
                    ? <EyeInvisibleOutlined style={{ color: 'var(--text-tertiary)', cursor: 'pointer' }} onClick={hideMailPwd} />
                    : <EyeOutlined style={{ color: 'var(--text-tertiary)', cursor: 'pointer' }} onClick={revealMailPwd} />}
                </Tooltip>
                <Tooltip title="修改邮箱密码">
                  <EditOutlined
                    style={{ color: 'var(--text-tertiary)', cursor: 'pointer' }}
                    onClick={() => { setMailPwdEditing(true); setMailPwdDraft('') }}
                  />
                </Tooltip>
              </div>
            )}
          </div>
          <div>
            <div style={{ fontSize: 13, color: 'var(--text-secondary)', marginBottom: 6 }}>手机号</div>
            {editingField === 'phone' ? (
              <Space.Compact style={{ width: '100%' }}>
                <Input value={editingValue} onChange={(e) => setEditingValue(e.target.value)} placeholder="11位手机号" maxLength={11} onPressEnter={saveContact} />
                <Button type="primary" loading={savingContact} onClick={saveContact}>确定</Button>
              </Space.Compact>
            ) : (
              <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <PhoneOutlined style={{ color: 'var(--text-tertiary)' }} />
                <span style={{ color: 'var(--text-primary)' }}>{profile.phone || '—'}</span>
                <Tooltip title="修改手机号"><EditOutlined style={{ color: 'var(--text-tertiary)', cursor: 'pointer' }} onClick={() => startEdit('phone')} /></Tooltip>
              </div>
            )}
          </div>
        </Card>
        <Card title="用量统计" className="profile-card profile-usage-card">
          {usage && (
            <>
              <div className="profile-stats">
                <Statistic title="会话数" value={usage.sessions} />
                <Statistic title="消息数" value={usage.messages} />
                <Statistic title="Token 总量" value={(usage.total_tokens / 1000000).toFixed(3)} suffix="M" />
                <Statistic title="工具调用" value={usage.tool_calls} />
              </div>
              <div className="profile-quota">
                <div className="profile-quota-label">
                  文件存储：{fmtBytes(usage.files_bytes)} / {fmtBytes(usage.files_quota_bytes)}（{usage.files_count} 个文件）
                </div>
                <Progress percent={quotaPct} size="small" />
              </div>
            </>
          )}
        </Card>
      </div>
    </div>
  )
}
