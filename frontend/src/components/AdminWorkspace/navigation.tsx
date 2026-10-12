import { ApiOutlined, AuditOutlined, DatabaseOutlined, DashboardOutlined, DeploymentUnitOutlined, RobotOutlined, TeamOutlined, ToolOutlined } from '@ant-design/icons'
import { hasModulePerm, hasPerm } from '../../constants/permissions'

export const adminDestinations = [
  { group: '组织与人员', label: '人员信息', path: '/admin/people', description: '人员档案、账号登录与角色授权', icon: TeamOutlined, module: 'org_manage' },
  { group: 'AI 资源', label: '企业模型', path: '/admin/models', description: '管理网关模型及使用权限', icon: DatabaseOutlined, adminOnly: true },
  { group: 'AI 资源', label: 'Agent 管理', path: '/admin/agents', description: '管理助手配置与资源绑定', icon: RobotOutlined, module: 'agent_manage' },
  { group: 'AI 资源', label: '技能社区审核', path: '/admin/skill-community', description: '投稿审核与社区安全治理', icon: ToolOutlined, adminOnly: true },
  { group: 'AI 资源', label: 'Skill 管理', path: '/admin/skills', description: '维护技能与技能包', icon: ToolOutlined, module: 'skill_manage' },
  { group: 'AI 资源', label: 'MCP 工具', path: '/admin/mcp', description: '管理服务连接与工具授权', icon: ApiOutlined, module: 'mcp_manage' },
  { group: '用户数据', label: '用户记忆', path: '/admin/memories', description: '查看来源、处理候选与审核', icon: DatabaseOutlined, module: 'memory_manage' },
  { group: '运营与治理', label: '运营管理', path: '/admin/operations', description: '查看运行情况与待处理事项', icon: DashboardOutlined, module: 'ops_manage' },
  { group: '运营与治理', label: '管理审计', path: '/admin/audit', description: '查询管理操作与敏感访问', icon: AuditOutlined, permission: 'ops_manage.audit' },
  { group: '运营与治理', label: '调用链', path: '/admin/traces', description: '追踪调用过程与工具结果', icon: DeploymentUnitOutlined, permission: 'ops_manage.traces' },
]

export function visibleAdminDestinations(user: { role?: string; permissions?: string[] } | null) {
  return adminDestinations.filter((item) => user?.role === 'admin' ||
    (!item.adminOnly && (item.permission ? hasPerm(user, item.permission) : item.module ? hasModulePerm(user, item.module) : false)))
}
