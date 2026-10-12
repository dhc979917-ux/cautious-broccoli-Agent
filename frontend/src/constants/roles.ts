// 角色显示常量（全前端唯一来源）

export const ROLE_COLORS: Record<string, string> = {
  viewer: 'green',
  operator: 'blue',
  admin: 'red',
}

export const ROLE_LABELS: Record<string, string> = {
  viewer: '观察者',
  operator: '操作员',
  admin: '管理员',
}

export const ROLE_OPTIONS = [
  { value: 'viewer', label: '观察者 (viewer)' },
  { value: 'operator', label: '操作员 (operator)' },
  { value: 'admin', label: '管理员 (admin)' },
]
