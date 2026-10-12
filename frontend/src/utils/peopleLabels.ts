import type { Department, RoleAssignment } from '../api/people'
import { PERM_LABEL_MAP } from '../constants/permissions'

export function permissionLabels(codes: string[]): string {
  return codes.map((code) => PERM_LABEL_MAP[code] || '未识别权限（请检查模板）').join('、') || '未设置附加权限'
}

export function roleScopeLabel(assignment: RoleAssignment, departments: Department[]): string {
  const labels: Record<string, string> = { global: '全部数据', department: '人员所在部门', self: '仅本人', resource: '指定资源', departments: '指定部门' }
  if (assignment.scope_type === 'global' || assignment.scope_type === 'self' || assignment.scope_type === 'department') return labels[assignment.scope_type]
  let scope: unknown
  try { scope = JSON.parse(assignment.scope_json || '[]') } catch { return '数据范围待核对' }
  if (Array.isArray(scope) && scope.length) {
    if (assignment.scope_type === 'departments') return '指定部门：' + scope.map((id) => departments.find((department) => department.id === Number(id))?.name || `部门编号 ${id}`).join('、')
    return '指定资源：' + scope.map((item: any) => typeof item === 'object' && item ? `${({ agent: '助手', skill: '技能', package: '技能包', knowledge: '知识库' } as Record<string, string>)[item.type] || '资源'} ${item.id}` : `资源编号 ${item}`).join('、')
  }
  if (scope && typeof scope === 'object' && !Array.isArray(scope)) {
    const resource = scope as { type?: string; ids?: number[] }
    const type = ({ agent: '助手', skill: '技能', package: '技能包', knowledge: '知识库', memory: '记忆' } as Record<string, string>)[resource.type || ''] || '资源'
    return `指定${type}：${resource.ids?.join('、') || '未指定'}`
  }
  return labels[assignment.scope_type] || '数据范围待核对'
}
