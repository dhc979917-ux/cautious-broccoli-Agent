# 阶段 1：组织、人员与角色

此代码基于 52 已部署的阶段 0 修订开发；未接入目录或单点登录。目录适配器只定义接口，`source + external_id` 唯一标识外部账号；历史用户这些字段保持空值。

## 数据与发布顺序

1. 在**获批的 52 发布窗口**确认实际运行镜像和 Gitea 提交，并对 PostgreSQL 做新备份及隔离恢复验证；禁止操作其他服务器。
2. 在隔离数据库先运行 `python migrate_admin_stage1_v1.py`（只读检查），核对将新增的表、列及唯一索引；生产备份通过后运行 `python migrate_admin_stage1_v1.py --apply`，再次只读检查应显示 `up to date`。迁移只新增结构，不清除旧字段或授权。
3. 旧版应用可以读取完成增量迁移的表；先迁移，再部署使用新增 ORM 字段的新版后端，避免启动时对缺失列的查询。前端随新后端上线；保持旧入口 `/users`、`/admin/users`、`/admin`、`/mcp` 可访问。发布中需核对只有受影响服务重启，PostgreSQL、Redis 及运行数据挂载不变。
4. `GOVERNANCE_RBAC_ENABLED` 默认关闭；在验证管理员、分页、旧授权与登录后，另行获批打开范围权限。关闭开关不影响旧角色和 Agent/Skill 授权。回退应用时保留新增表/列与旧授权表，不做破坏性回滚。

## 接口与安全约束

- `GET /api/v1/admin/people` 使用服务端分页、过滤及排序；`GET /api/v1/users/` 继续返回原来的数组形状。
- `GET /api/v1/admin/people/{id}` 返回人员详情、角色分配、Agent/Skill 授权摘要和会话/消息统计，不返回密码或密钥。跨部门读取、敏感读取的审计失败都必须拒绝。
- `PUT /api/v1/admin/people/{id}`、`PUT .../resources`、角色分配和撤销均须传 `revision`；旧版本返回 409。仅管理员可以更改基础角色、账号状态、外部身份及资源/角色授权；有 `org_manage.update` 范围授权的非管理员仅能改本范围内的职位和工号。
- 部门维护及角色模板写入仅管理员可执行，拒绝部门环和最后一位管理员被停用。基础角色仍生效；范围模板按统一权限解析器参与鉴权，无目标上下文的旧管理 API 不接受范围角色越权。角色、模板和授权变更在同一事务提升令牌版本，再通知在线会话下线。
- 人工组织字段的优先级、外部账号自动绑定、离职同步及账号冲突策略须在接入具体身份源时另行设计；目前不存在自动同步任务。

隔离验收：`python -m unittest _test_admin_stage0 _test_admin_stage1_api _test_admin_stage1_people _test_admin_stage1_scopes _test_admin_stage1_schema`；前端执行 `npm run build`，再用合成账号和数据检查页面及原有路由。
