from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey, Index
from sqlalchemy.sql import func
from app.database import Base
from sqlalchemy import Text


'''
用户表：
Integer：整数列
String(50)：字符串列，最大长度50
Boolean：布尔列（数据库通常存 tinyint 或 boolean）
DateTime(...)：日期时间列，可带参数 timezone=True
'''


class User(Base):
    # 指定这个模型对应数据库里的哪张表
    __tablename__ = "users"
    
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, index=True, nullable=False)
    # 登录账号（唯一）。存量用户由迁移回填为原 username；新用户注册时自行填写
    account = Column(String(50), unique=True, index=True)
    # 存储的是 bcrypt 哈希后的密码，不是明文
    password_hash = Column(String(255), nullable=False)
    email = Column(String(100), nullable=True)
    # 手机号（11 位数字），注册时必填
    phone = Column(String(20), nullable=True)
    # 头像文件名（存于 uploads/avatars/avatar_{id}.{ext}；NULL=未设置，前端显示默认图标）
    avatar = Column(String(255), nullable=True)
    # Agent 头像文件名（存于 uploads/avatars/agent_avatar_{id}.{ext}；NULL=未设置，聊天页显示默认机器人图标）
    agent_avatar = Column(String(255), nullable=True)
    # 邮箱密码（邮箱平台的独立密码/授权码，供邮件 MCP 收发信使用）。
    # 可逆混淆存储（core.secrets），NULL=未配置。注意：这不是平台登录密码。
    mail_password = Column(String(255), nullable=True)
    # 角色，三种取值：
    #   - admin：管理员，能管理用户、创建/删除 Agent
    #   - operator：操作员，能和被授权的 Agent 对话
    #   - viewer：查看者，只能看不能操作（预留角色，当前代码未使用）
    role = Column(String(20), default="operator")  # admin, operator, viewer
    # “启用/禁用”的开关，当管理员想禁用一个用户时，不真的从数据库里删掉记录，而是把 is_active 改成 False,保留历史数据.
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now()) # 创建时间(自动填入)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now()) # 更新时间(自动填入)
    last_active = Column(DateTime, nullable=True)  # last active (heartbeat middleware)
    last_login = Column(DateTime(timezone=True), nullable=True) # 最后登录时间（手动更新）
    # 初始密码/管理员重置后为 True；后端会阻止访问业务接口，直到用户完成改密。
    must_change_password = Column(Boolean, nullable=False, default=False, server_default="false")
    password_initialized_at = Column(DateTime(timezone=True), nullable=True)
    password_changed_at = Column(DateTime(timezone=True), nullable=True)
    # 细粒度功能权限（JSON 列表字符串，如 ["mcp_manage","kb_manage"]）。
    # admin 不依赖此字段（天然全权限）；为空/NULL 表示无任何附加权限。
    permissions = Column(Text, nullable=True)
    # 令牌版本号：签发 JWT 时写入（claim "tv"），校验时与库里的值比对。
    # 管理员改角色/权限/授权/启用状态后自增一次，该用户已签发的全部 token 立刻失效
    # （见 core/deps.py 的校验与 services/session_guard.py 的吊销流程），不必等自然过期。
    token_version = Column(Integer, nullable=False, default=0, server_default="0")
    department_id = Column(Integer, ForeignKey("departments.id", ondelete="SET NULL"), nullable=True)
    job_title = Column(String(120), nullable=True)
    external_id = Column(String(160), nullable=True)
    source = Column(String(80), nullable=True)
    employment_status = Column(String(20), nullable=True)
    revision = Column(Integer, nullable=False, default=0, server_default="0")

    @property
    def permissions_list(self):
        """解析 permissions JSON 为列表；解析逻辑统一在 roles.parse_permissions"""
        from app.core.roles import parse_permissions
        return parse_permissions(self.permissions)

    @property
    def avatar_url(self):
        """头像访问地址（免鉴权）；URL 固定，缓存策略由读取接口的 Cache-Control: no-cache 控制"""
        if not self.avatar:
            return None
        return f"/api/v1/users/avatar/{self.id}"

    @property
    def agent_avatar_url(self):
        """Agent 头像访问地址（免鉴权）；未设置返回 None，前端显示默认机器人图标"""
        if not self.agent_avatar:
            return None
        return f"/api/v1/users/agent-avatar/{self.id}"


Index("uq_users_source_external", User.source, User.external_id, unique=True)
