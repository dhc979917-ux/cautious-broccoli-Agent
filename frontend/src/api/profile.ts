import request from './request'

// 个人信息（自助）API

export interface MyProfile {
  id: number
  username: string
  account?: string
  email?: string | null
  phone?: string | null
  avatar_url?: string | null
  agent_avatar_url?: string | null
  role: string
  is_active: boolean
  created_at?: string
  last_login?: string | null
  // 邮箱密码（邮件 MCP 用）是否已配置；密码本身不在本接口返回
  has_mail_password?: boolean
  must_change_password?: boolean
}

export interface MyUsage {
  sessions: number
  messages: number
  input_tokens: number
  output_tokens: number
  total_tokens: number
  tool_calls: number
  files_count: number
  files_bytes: number
  files_quota_bytes: number
}

export async function getMyProfile(): Promise<MyProfile> {
  const res = await request.get('/users/me')
  return res.data
}

// 上传个人头像（后端存盘并返回 avatar_url）
export async function uploadMyAvatar(file: Blob | File): Promise<{ avatar_url: string }> {
  const form = new FormData()
  form.append('file', file, file instanceof File ? file.name : 'avatar.png')
  const res = await request.post('/users/me/avatar', form)
  return res.data
}

// 上传 Agent 头像（聊天页 AI 头像；设置一次即默认，重复上传覆盖）
export async function uploadMyAgentAvatar(file: Blob | File): Promise<{ agent_avatar_url: string }> {
  const form = new FormData()
  form.append('file', file, file instanceof File ? file.name : 'agent-avatar.png')
  const res = await request.post('/users/me/agent-avatar', form)
  return res.data
}

export async function updateMyProfile(data: { email?: string; phone?: string }) {
  const res = await request.put('/users/me', data)
  return res.data
}

// 查看本人邮箱密码明文（仅"眼睛"按钮按需调用；密码不在 profile 接口里）
export async function getMyMailPassword(): Promise<{ mail_password: string }> {
  const res = await request.get('/users/me/mail-password')
  return res.data
}

// 保存本人邮箱密码（邮箱平台的独立密码/授权码；不会修改邮箱平台的密码）
export async function setMyMailboxPassword(mailPassword: string) {
  const res = await request.post('/users/me/mailbox', { mail_password: mailPassword })
  return res.data
}

export async function changeMyPassword(oldPassword: string, newPassword: string) {
  const res = await request.post('/users/me/password', {
    old_password: oldPassword,
    new_password: newPassword,
  })
  return res.data
}

export async function getMyUsage(): Promise<MyUsage> {
  const res = await request.get('/users/me/usage')
  return res.data
}
