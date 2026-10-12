export const INITIAL_PASSWORD = 'Initial@123456'

export function passwordPolicyError(password: string, account?: string): string | null {
  if (password.length < 8) return '密码至少 8 位'
  if (!/[A-Z]/.test(password)) return '密码必须包含大写字母'
  if (!/[a-z]/.test(password)) return '密码必须包含小写字母'
  if (!/\d/.test(password)) return '密码必须包含数字'
  if (password === INITIAL_PASSWORD) return '新密码不能与初始密码相同'
  const normalized = (account || '').trim().toLowerCase()
  if (normalized.length >= 4 && password.toLowerCase().includes(normalized)) return '密码不能包含完整登录账号'
  return null
}
