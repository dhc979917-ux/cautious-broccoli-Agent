from sqlalchemy.orm import Session
from datetime import datetime, timezone
from app.models.user import User
from app.models.user_agent_access import UserAgentAccess
from app.core.avatars import global_agent_avatar_url
from app.core.security import verify_password, get_password_hash, create_access_token, compute_expires_in_seconds
from app.schemas.auth import LoginRequest, LoginResponse, UserInfo
from app.config import settings
from app.services.organization_access import navigation_permissions
from app.core.password_policy import DEFAULT_INITIAL_PASSWORD


'''人的管理：登录认证（验密码→查权限→生成JWT）、创建用户、授予 Agent 访问权限'''


class AuthService:
    def __init__(self, db: Session):
        self.db = db
    
    def authenticate(self, login_data: LoginRequest) -> LoginResponse:
        """用户登录认证"""
        # 按账号查找用户(数据库查出来的account=？用户输入后经过校验的account)
        user = self.db.query(User).filter(User.account == login_data.account).first()
        
        if not user:
            raise ValueError("账号或密码错误")
        
        if not user.is_active:
            raise ValueError("账号已被禁用")
        
        # 验证密码
        if not verify_password(login_data.password, user.password_hash):
            raise ValueError("账号或密码错误")
        
        # 更新最后登录时间
        user.last_login = datetime.now()
        self.db.commit()
        
        # 获取用户的Agent权限列表
        agent_access = self.db.query(UserAgentAccess).filter(
            UserAgentAccess.user_id == user.id
        ).all()
        agent_permissions = [str(access.agent_id) for access in agent_access]
        
        # 创建JWT令牌
        token_data = {
            "sub": str(user.id),
            "username": user.username,
            "role": user.role,
            "agent_permissions": agent_permissions,
            # 令牌版本号：管理员吊销会话后自增，旧 token 校验不通过（见 core/deps.py）
            "tv": int(user.token_version or 0),
        }
        access_token = create_access_token(data=token_data)
        
        # 构造响应
        user_info = UserInfo(
            id=user.id,
            username=user.username,
            account=user.account or "",
            email=user.email,
            phone=user.phone,
            avatar_url=user.avatar_url,
            agent_avatar_url=global_agent_avatar_url(),
            role=user.role,
            agent_permissions=agent_permissions,
            permissions=navigation_permissions(self.db, user),
            must_change_password=bool(user.must_change_password),
        )
        
        return LoginResponse(
            access_token=access_token,
            token_type="bearer",
            expires_in=compute_expires_in_seconds(),  # 与配置保持一致
            user=user_info
        )
    
    def create_user(self, account: str, username: str, password: str, phone: str = None, email: str = None, role: str = "operator") -> User:
        """创建新用户"""
        # 检查账号是否已存在
        existing = self.db.query(User).filter(User.account == account).first()
        if existing:
            raise ValueError("账号已存在")
        # 检查用户名（真实姓名）是否已存在
        existing = self.db.query(User).filter(User.username == username).first()
        if existing:
            raise ValueError("用户名已存在")
        
        # 创建用户
        user = User(
            account=account,
            username=username,
            password_hash=get_password_hash(DEFAULT_INITIAL_PASSWORD),
            phone=phone,
            email=email,
            role=role,
            must_change_password=True,
            password_initialized_at=datetime.now(timezone.utc),
        )
        self.db.add(user)
        self.db.commit()
        self.db.refresh(user)
        return user
    
    def grant_agent_access(self, user_id: int, agent_id: int, access_level: str = "execute"):
        """授予用户访问某个Agent的权限"""
        # 检查是否已有权限
        existing = self.db.query(UserAgentAccess).filter(
            UserAgentAccess.user_id == user_id,
            UserAgentAccess.agent_id == agent_id
        ).first()
        
        if existing:
            return
        
        # 添加权限
        access = UserAgentAccess(
            user_id=user_id,
            agent_id=agent_id,
            access_level=access_level
        )
        self.db.add(access)
        self.db.commit()
