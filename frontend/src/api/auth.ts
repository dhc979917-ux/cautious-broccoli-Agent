import request from './request'

export interface LoginParams {
  account: string
  password: string
}

export interface UserInfo {
  id: number
  username: string
  account?: string
  email?: string
  phone?: string
  avatar_url?: string | null
  agent_avatar_url?: string | null
  role: string
  agent_permissions: string[]
  permissions: string[]
  must_change_password: boolean
}

export interface LoginResponse {
  access_token: string
  token_type: string
  expires_in: number
  user: UserInfo
}

// 登录
export async function login(params: LoginParams): Promise<LoginResponse> {
  const res = await request.post('/auth/login', params)
  return res.data
}

// 注册（登录页自助注册与管理员"新建用户"共用）
export interface RegisterParams {
  account: string
  username: string
  password: string
  phone: string
  email?: string | null
}

export async function registerUser(params: RegisterParams) {
  const res = await request.post('/auth/register', params)
  return res.data
}

// 获取当前用户信息
export async function getMe(): Promise<UserInfo> {
  const res = await request.get('/auth/me')
  return res.data
}

// 忘记密码：校验 登录账号+绑定邮箱 后发送重置验证码（公开接口）
export async function sendPasswordResetCode(username: string, email: string): Promise<{ message: string }> {
  const res = await request.post('/auth/password/send-code', { username, email })
  return res.data
}

// 忘记密码：登录账号+邮箱+验证码+新密码 重置（公开接口）
export async function resetPasswordByCode(username: string, email: string, code: string, newPassword: string): Promise<{ message: string }> {
  const res = await request.post('/auth/password/reset', { username, email, code, new_password: newPassword })
  return res.data
}
