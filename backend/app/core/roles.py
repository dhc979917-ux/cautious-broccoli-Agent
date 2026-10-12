"""角色等级映射（全项目唯一定义点）

角色等级：viewer(1) < operator(2) < admin(3)
所有需要比较角色权限的地方统一从这里导入，不要各自重复定义。
"""

import json

ROLE_LEVELS = {
    "viewer": 1,
    "operator": 2,
    "admin": 3,
}

# 允许自助注册的默认角色取值（admin 永远不允许自助注册获得）
SELF_REGISTER_ROLES = ("viewer", "operator")

# 功能权限码（细粒度权限，可分配给非 admin；admin 天然拥有全部权限）
# 树形结构：模块 -> 操作 -> 子操作（可多层）。存储时权限码为路径码：
#   - 存节点码 = 拥有该节点整棵子树（"mcp_manage" = MCP 全部操作，兼容旧数据）
#   - 存叶子/中间节点路径码 = 只拥有对应子树（"file_all.manage.rename" = 仅重命名他人文件）
PERMISSION_TREE = {
    'ops_manage': {'label': '运营与审计', 'children': {
        'view': {'label': '查看运营聚合', 'children': {}},
        'traces': {'label': '查看授权范围调用链', 'children': {}},
        'audit': {'label': '查看脱敏管理审计', 'children': {}},
        'policy': {'label': '维护告警与留存政策', 'children': {}},
        'cleanup': {'label': '申请与审核留存清理', 'children': {}},
    }},
    "memory_manage": {
        "label": "用户记忆治理",
        "children": {
            "view": {"label": "查看授权范围记忆", "children": {}},
            "create": {"label": "创建融合候选", "children": {}},
            "review": {"label": "审核融合候选", "children": {}},
            "publish": {"label": "发布及索引对账", "children": {}},
            "policy": {"label": "维护记忆留存政策", "children": {}},
        },
    },
    "mcp_manage": {
        "label": "MCP 服务管理",
        "children": {
            "create": {"label": "添加 MCP", "children": {}},
            "update": {"label": "编辑 MCP", "children": {}},
            "delete": {"label": "删除 MCP", "children": {
                "soft": {"label": "软删除 MCP", "children": {}},
                "hard": {"label": "彻底删除 MCP", "children": {}},
            }},
            "restore": {"label": "恢复 MCP", "children": {}},
            "toggle": {"label": "启用/停用", "children": {}},
            "test": {"label": "测试连接", "children": {}},
        },
    },
    "kb_manage": {
        "label": "知识库管理",
        "children": {
            "kb_create": {"label": "新建知识库", "children": {}},
            "kb_update": {"label": "编辑知识库", "children": {}},
            "kb_delete": {"label": "删除知识库", "children": {}},
            "doc_upload": {"label": "上传文档", "children": {}},
            "doc_delete": {"label": "删除文档", "children": {}},
        },
    },
    "file_all": {
        "label": "文件中心（全用户）",
        "children": {
            "view": {"label": "查看全部文件与全局统计", "children": {}},
            "download": {
                "label": "下载他人文件",
                "children": {
                    "single": {"label": "单文件下载", "children": {}},
                    "batch": {"label": "批量下载", "children": {}},
                },
            },
            "manage": {
                "label": "管理他人文件",
                "children": {
                    "rename": {"label": "重命名", "children": {}},
                    "soft_delete": {"label": "删除（移入回收站）", "children": {}},
                    "restore": {"label": "从回收站恢复", "children": {}},
                },
            },
            "sync": {"label": "磁盘对账", "children": {}},
            "hard_delete": {"label": "彻底删除", "children": {}},
        },
    },
    "agent_manage": {
        "label": "Agent 管理",
        "children": {
            "create": {"label": "新建 Agent", "children": {}},
            "update": {"label": "编辑 Agent", "children": {}},
            "delete": {
                "label": "Agent 处置",
                "children": {
                    "soft": {"label": "删除", "children": {}},
                    "restore": {"label": "恢复", "children": {}},
                    "hard": {"label": "彻底删除", "children": {}},
                },
            },
        },
    },
    "skill_manage": {
        "label": "Skill 管理",
        "children": {
            "create": {"label": "新建技能", "children": {}},
            "update": {"label": "编辑技能", "children": {}},
            "delete": {
                "label": "技能处置",
                "children": {
                    "soft": {"label": "删除", "children": {}},
                    "restore": {"label": "恢复", "children": {}},
                    "hard": {"label": "彻底删除", "children": {}},
                },
            },
            "package": {
                "label": "技能包管理",
                "children": {
                    "upload": {"label": "上传技能包", "children": {}},
                    "update": {"label": "编辑技能包", "children": {}},
                    "soft": {"label": "删除", "children": {}},
                    "restore": {"label": "恢复", "children": {}},
                    "hard": {"label": "彻底删除", "children": {}},
                },
            },
            "refresh": {"label": "刷新技能缓存", "children": {}},
        },
    },
    "user_manage": {
        "label": "用户管理",
        "children": {
            "delete": {
                "label": "用户处置",
                "children": {
                    "hard": {"label": "彻底删除", "children": {}},
                },
            },
        },
    },
    "org_manage": {
        "label": "组织与人员",
        "children": {
            "view": {"label": "查看人员", "children": {}},
            "update": {"label": "编辑人员", "children": {}},
            "grant": {"label": "分配角色与资源", "children": {}},
            "department": {"label": "维护部门", "children": {}},
            "template": {"label": "维护角色模板", "children": {}},
        },
    },
    "monitor_manage": {
        "label": "监控与运维管理",
        "children": {},
    },
    "schedule_manage": {
        "label": "调度任务管理",
        "children": {
            "create": {"label": "新建调度任务", "children": {}},
            "update": {"label": "编辑调度任务", "children": {}},
            "delete": {
                "label": "任务处置",
                "children": {
                    "soft": {"label": "删除（移入回收站）", "children": {}},
                    "restore": {"label": "恢复", "children": {}},
                    "hard": {"label": "彻底删除", "children": {}},
                },
            },
            "toggle": {"label": "启用/停用", "children": {}},
            "run": {"label": "手动触发执行", "children": {}},
            "view_all": {"label": "查看全部任务与执行历史", "children": {}},
        },
    },
}


def all_permission_codes(tree=None, prefix="") -> set:
    """全部合法权限路径码（模块码 + 各层节点码），供保存时校验"""
    codes = set()
    for code, node in (tree if tree is not None else PERMISSION_TREE).items():
        full = f"{prefix}{code}"
        codes.add(full)
        if node.get("children"):
            codes |= all_permission_codes(node["children"], full + ".")
    return codes


def covers(stored: str, requested: str) -> bool:
    """已存权限码是否覆盖请求码：相等，或已存码是请求码的祖先（节点码=整棵子树）"""
    return stored == requested or requested.startswith(stored + ".")


def parse_permissions(raw) -> list:
    """把数据库里存的 permissions JSON 字符串解析成权限码列表（解析逻辑全项目只在这一处）。
    入参可以是 None / JSON 字符串 / 已是列表；解析失败一律视为空列表。"""
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(p) for p in raw]
    if isinstance(raw, str):
        if not raw.strip():
            return []
        try:
            v = json.loads(raw)
            return [str(p) for p in v] if isinstance(v, list) else []
        except Exception:
            return []
    return []
