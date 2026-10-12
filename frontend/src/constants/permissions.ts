// 细粒度功能权限树（全前端唯一来源，与后端 app/core/roles.py 的 PERMISSION_TREE 一一对应）
// 权限码为路径码：存节点码 = 拥有该节点整棵子树（"mcp_manage" = MCP 全部操作，兼容旧数据）；
// 存路径码 = 只拥有对应子树（"file_all.manage.rename" = 仅重命名他人文件）

export interface PermNode {
  label: string
  children?: Record<string, PermNode>
}

export const PERMISSION_TREE: Record<string, PermNode> = {
  ops_manage: { label: '运营与审计', children: {
    view: { label: '查看运营聚合' }, traces: { label: '查看授权范围调用链' },
    audit: { label: '查看脱敏管理审计' }, policy: { label: '维护告警与留存政策' },
    cleanup: { label: '申请与审核留存清理' },
  } },
  memory_manage: {
    label: '用户记忆治理',
    children: { view: { label: '查看授权范围记忆' }, create: { label: '创建融合候选' },
      review: { label: '审核融合候选' }, publish: { label: '发布及索引对账' }, policy: { label: '维护记忆留存政策' } },
  },
  mcp_manage: {
    label: 'MCP 服务管理',
    children: {
      create: { label: '添加 MCP' },
      update: { label: '编辑 MCP' },
      delete: { label: '删除 MCP', children: { soft: { label: '软删除 MCP' }, hard: { label: '彻底删除 MCP' } } },
      restore: { label: '恢复 MCP' },
      toggle: { label: '启用/停用' },
      test: { label: '测试连接' },
    },
  },
  kb_manage: {
    label: '知识库管理',
    children: {
      kb_create: { label: '新建知识库' },
      kb_update: { label: '编辑知识库' },
      kb_delete: { label: '删除知识库' },
      doc_upload: { label: '上传文档' },
      doc_delete: { label: '删除文档' },
    },
  },
  file_all: {
    label: '文件中心（全用户）',
    children: {
      view: { label: '查看全部文件与全局统计' },
      download: {
        label: '下载他人文件',
        children: {
          single: { label: '单文件下载' },
          batch: { label: '批量下载' },
        },
      },
      manage: {
        label: '管理他人文件',
        children: {
          rename: { label: '重命名' },
          soft_delete: { label: '删除（移入回收站）' },
          restore: { label: '从回收站恢复' },
        },
      },
      sync: { label: '磁盘对账' },
      hard_delete: { label: '彻底删除' },
    },
  },
  agent_manage: {
    label: 'Agent 管理',
    children: {
      create: { label: '新建 Agent' },
      update: { label: '编辑 Agent' },
      delete: {
        label: 'Agent 处置',
        children: {
          soft: { label: '删除' },
          restore: { label: '恢复' },
          hard: { label: '彻底删除' },
        },
      },
    },
  },
  skill_manage: {
    label: 'Skill 管理',
    children: {
      create: { label: '新建技能' },
      update: { label: '编辑技能' },
      delete: {
        label: '技能处置',
        children: {
          soft: { label: '删除' },
          restore: { label: '恢复' },
          hard: { label: '彻底删除' },
        },
      },
      package: {
        label: '技能包管理',
        children: {
          upload: { label: '上传技能包' },
          update: { label: '编辑技能包' },
          soft: { label: '删除' },
          restore: { label: '恢复' },
          hard: { label: '彻底删除' },
        },
      },
      refresh: { label: '刷新技能缓存' },
    },
  },
  org_manage: {
    label: '组织与人员',
    children: {
      view: { label: '查看人员' },
      update: { label: '编辑人员' },
      grant: { label: '分配角色与资源' },
      department: { label: '维护部门' },
      template: { label: '维护角色模板' },
    },
  },
  monitor_manage: {
    label: '监控与运维管理',
  },
  schedule_manage: {
    label: '调度任务管理',
    children: {
      create: { label: '新建调度任务' },
      update: { label: '编辑调度任务' },
      delete: {
        label: '任务处置',
        children: {
          soft: { label: '删除（移入回收站）' },
          restore: { label: '恢复' },
          hard: { label: '彻底删除' },
        },
      },
      toggle: { label: '启用/停用' },
      run: { label: '手动触发执行' },
      view_all: { label: '查看全部任务与执行历史' },
    },
  },
}

export const MODULES = Object.keys(PERMISSION_TREE)

/** 扁平映射：任意层级路径码 -> 该节点自身标签（人员信息页 Tag 展示用） */
export const PERM_LABEL_MAP: Record<string, string> = (() => {
  const map: Record<string, string> = {}
  const walk = (children: Record<string, PermNode>, prefix: string) => {
    for (const [code, node] of Object.entries(children)) {
      const key = prefix ? `${prefix}.${code}` : code
      map[key] = node.label
      if (node.children) walk(node.children, key)
    }
  }
  walk(PERMISSION_TREE, '')
  return map
})()

/** 已存权限码是否覆盖请求码：相等，或已存码是请求码的祖先（节点码=整棵子树） */
export function permCovers(stored: string, requested: string): boolean {
  return stored === requested || requested.startsWith(stored + '.')
}

interface PermUser {
  role?: string
  permissions?: string[]
}

/** 操作级判断：请求某个具体权限码（含路径码）。admin 天然全权限 */
export function hasPerm(user: PermUser | null | undefined, perm: string): boolean {
  if (!user) return false
  if (user.role === 'admin') return true
  return (user.permissions || []).some((s) => permCovers(s, perm))
}

/** 模块级判断：该模块子树内拥有任意权限（菜单/页面显隐、列表接口用） */
export function hasModulePerm(user: PermUser | null | undefined, module: string): boolean {
  if (!user) return false
  if (user.role === 'admin') return true
  return (user.permissions || []).some((s) => s === module || s.startsWith(module + '.'))
}

/** 是否拥有任意一项功能权限（决定是否展示管理后台入口） */
export function hasAnyPerm(user: PermUser | null | undefined): boolean {
  if (!user) return false
  return MODULES.some((m) => hasModulePerm(user, m))
}

// ===== 树形勾选 <-> 存储码 互转（编辑弹窗用） =====

function collectSubtreeKeys(children: Record<string, PermNode>, prefix: string, out: string[]) {
  for (const [code, node] of Object.entries(children)) {
    const key = prefix ? `${prefix}.${code}` : code
    out.push(key)
    if (node.children) collectSubtreeKeys(node.children, key, out)
  }
}

/** 存储码展开为勾选 keys（含节点自身与全部后代），供 antd Tree 回显 */
export function expandToCheckedKeys(stored: string[]): string[] {
  const out: string[] = []
  for (const code of stored || []) {
    const parts = code.split('.')
    let node: PermNode | undefined = PERMISSION_TREE[parts[0]]
    if (!node) continue
    let prefix = parts[0]
    let ok = true
    for (let i = 1; i < parts.length; i++) {
      const child: PermNode | undefined = node.children?.[parts[i]]
      if (!child) { ok = false; break }
      node = child
      prefix = `${prefix}.${parts[i]}`
    }
    if (!ok) continue
    out.push(prefix)
    if (node?.children) collectSubtreeKeys(node.children, prefix, out)
  }
  return [...new Set(out)]
}

function collapseChildren(children: Record<string, PermNode>, prefix: string, checkedSet: Set<string>): { full: boolean; codes: string[] } {
  const codes: string[] = []
  let full = true
  for (const [code, node] of Object.entries(children)) {
    const key = `${prefix}.${code}`
    if (node.children) {
      const r = collapseChildren(node.children, key, checkedSet)
      if (r.full) codes.push(key)
      else codes.push(...r.codes)
      full = full && r.full
    } else {
      if (checkedSet.has(key)) codes.push(key)
      else full = false
    }
  }
  return { full, codes }
}

/** 勾选 keys 收敛为最简存储码：子树全选时只存父节点码 */
export function collapseToCodes(checkedKeys: string[]): string[] {
  const set = new Set(checkedKeys)
  const out: string[] = []
  for (const [module, node] of Object.entries(PERMISSION_TREE)) {
    if (!node.children) {
      if (set.has(module)) out.push(module)
      continue
    }
    const r = collapseChildren(node.children, module, set)
    if (r.full) out.push(module)
    else out.push(...r.codes)
  }
  return out
}

/** antd Tree 数据（key = 路径码） */
export function buildPermTreeData(): { key: string; title: string; children?: any[] }[] {
  const toNodes = (children: Record<string, PermNode>, prefix: string): any[] =>
    Object.entries(children).map(([code, node]) => {
      const key = prefix ? `${prefix}.${code}` : code
      return {
        key,
        title: node.label,
        children: node.children ? toNodes(node.children, key) : undefined,
      }
    })
  return toNodes(PERMISSION_TREE, '')
}
