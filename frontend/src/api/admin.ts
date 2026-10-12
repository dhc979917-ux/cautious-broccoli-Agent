import request from './request'

// Agent 管理 API
export async function getAllAgents() {
  const res = await request.get('/agents/all')
  return res.data
}

export async function createAgent(data: {
  name: string
  description?: string
  agent_type: string
  model_config_name?: string
  system_prompt?: string
  config?: Record<string, any>
}) {
  const res = await request.post('/agents/', data)
  return res.data
}

export async function updateAgent(agentId: number, data: {
  name?: string
  description?: string
  system_prompt?: string
  model_config_name?: string
  config?: Record<string, any>
}) {
  const res = await request.put(`/agents/${agentId}`, data)
  return res.data
}

export async function deleteAgent(agentId: number) {
  const res = await request.delete(`/agents/${agentId}`)
  return res.data
}

export async function restoreAgent(agentId: number) {
  const res = await request.put(`/agents/${agentId}/restore`)
  return res.data
}

export async function hardDeleteAgent(agentId: number, username: string, password: string) {
  const res = await request.delete(`/agents/${agentId}/hard`, {
    data: { username, password }
  })
  return res.data
}

// Skill 管理 API
export async function getAllSkillsAdmin() {
  const res = await request.get('/skills/all')
  return res.data
}

export async function createSkill(data: {
  name: string
  display_name: string
  description?: string
  required_role: string
  is_hidden?: boolean
  allow_fork?: boolean
  fork_policy_inherited?: boolean
}) {
  const res = await request.post('/skills/', data)
  return res.data
}

export async function updateSkill(skillId: number, data: {
  display_name?: string
  description?: string
  required_role?: string
  is_hidden?: boolean
  allow_fork?: boolean
}) {
  const res = await request.put(`/skills/${skillId}`, data)
  return res.data
}

export async function deleteSkill(skillId: number) {
  const res = await request.delete(`/skills/${skillId}`)
  return res.data
}

export async function restoreSkill(skillId: number) {
  const res = await request.put(`/skills/${skillId}/restore`)
  return res.data
}

export async function hardDeleteSkill(skillId: number, username: string, password: string) {
  const res = await request.delete(`/skills/${skillId}/hard`, {
    data: { username, password }
  })
  return res.data
}

export async function refreshSkills() {
  const res = await request.post(`/skills/refresh`)
  return res.data
}

// 技能归属：可选技能 / 工具助手内置 / 数据分析师内置
export type SkillScope = 'optional' | 'dialog' | 'task'

// 切换归属是"剪切"语义：后端把技能目录移动到对应根目录并重扫注册，
// 因此调用方成功后必须重新拉一次技能列表（分组按 builtin 标志过滤）
export async function changeSkillScope(skillId: number, scope: SkillScope) {
  const res = await request.put(`/skills/${skillId}/scope`, { scope })
  return res.data
}
// 技能包管理 API
export interface SkillPackageInfo {
  id: number
  name: string
  display_name: string
  description?: string | null
  required_role?: string | null
  is_active: number
  // 磁盘身份与目录缺失标记：dir_missing=true 时"恢复"会被后端拒绝（409），前端据此提示重新上传
  rel_path?: string | null
  dir_missing?: boolean
  // 广场隐藏（管理员态）：true=对未授权非管理员隐藏整包
  is_hidden?: boolean
  allow_fork?: boolean
  fork_policy_inherited?: boolean
  created_at: string
}

export async function getAllPackagesAdmin(): Promise<SkillPackageInfo[]> {
  const res = await request.get('/skills/packages/all')
  return res.data
}

export async function updatePackage(packageId: number, data: {
  display_name?: string
  description?: string
  required_role?: string
  is_hidden?: boolean
  allow_fork?: boolean
}) {
  const res = await request.put(`/skills/packages/${packageId}`, data)
  return res.data
}

export async function deletePackage(packageId: number) {
  const res = await request.delete(`/skills/packages/${packageId}`)
  return res.data
}

export async function changePackageScope(packageId: number, scope: SkillScope) {
  return (await request.put(`/skills/packages/${packageId}/scope`, { scope })).data
}

export async function restorePackage(packageId: number) {
  const res = await request.put(`/skills/packages/${packageId}/restore`)
  return res.data
}

export async function hardDeletePackage(packageId: number, username: string, password: string) {
  const res = await request.delete(`/skills/packages/${packageId}/hard`, {
    data: { username, password }
  })
  return res.data
}
// 用户管理 API
// 彻底删除用户（硬删除，需管理员账号密码二次验证）
export async function hardDeleteUser(userId: number, username: string, password: string) {
  const res = await request.delete(`/users/${userId}/hard`, {
    data: { username, password }
  })
  return res.data
}

// 管理员一键重置用户密码为默认密码（管理员账号+密码两步确认，复用彻底删除模式）
export async function resetUserPassword(userId: number, username: string, password: string) {
  const res = await request.put(`/users/${userId}/reset-password`, { username, password })
  return res.data
}

export interface AdminUserInfo {
  id: number
  username: string
  account?: string
  email?: string
  phone?: string
  role: string
  is_active: boolean
  created_at: string
  last_login?: string
  agent_ids: number[]
  // Skill 授权（编辑弹窗回显；授权覆盖角色门槛与广场隐藏）
  skill_grant_skill_ids?: number[]
  skill_grant_package_ids?: number[]
  permissions: string[]
}

export async function getAllUsers(): Promise<AdminUserInfo[]> {
  const res = await request.get('/users/')
  return res.data
}

// 带排序的用户列表（人员信息页）
export async function getAllUsersSorted(sortBy: string, sortOrder: string): Promise<AdminUserInfo[]> {
  const res = await request.get('/users/', { params: { sort_by: sortBy, sort_order: sortOrder } })
  return res.data
}

export async function updateUser(userId: number, data: {
  email?: string
  username?: string
  account?: string
  role?: string
  is_active?: boolean
  agent_ids?: number[]
  skill_grant_skill_ids?: number[]
  skill_grant_package_ids?: number[]
  permissions?: string[]
}): Promise<AdminUserInfo> {
  const res = await request.put(`/users/${userId}`, data)
  return res.data
}

export interface SkillGrantOption {
  id: number
  name: string
  label: string
  package_id?: number | null
  is_hidden: boolean
}

export interface SkillGrantOptions {
  skills: SkillGrantOption[]
  packages: SkillGrantOption[]
}

export async function getSkillGrantOptions(): Promise<SkillGrantOptions> {
  const res = await request.get('/users/skill-grant-options')
  return res.data
}
