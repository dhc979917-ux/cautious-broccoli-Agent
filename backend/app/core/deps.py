from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi import Request
from sqlalchemy.orm import Session
from app.database import get_db
from app.core.security import decode_token
from app.models.user import User

'''FastAPI依赖注入（验证登录状态）'''


# Bearer Token认证
# 创建一个 HTTPBearer 实例。当它被用在 Depends(security) 中时，FastAPI 会：
#   1. 检查请求头里有没有 Authorization 字段
#   2. 检查格式是不是 Bearer xxxxx
#   3. 如果没有或格式不对，自动返回 403 Forbidden
#   4. 如果正确，把令牌提取到 credentials.credentials 里
security = HTTPBearer()

# 会话被吊销时返回的 401 文案：前端按 "SESSION_REVOKED" 前缀识别，
# 在登录页提示"权限已变更，请重新登录"（见 frontend/src/api/request.ts）
SESSION_REVOKED_DETAIL = "SESSION_REVOKED:登录状态已失效，请重新登录"

#  - Depends(security)：从请求头提取 JWT 令牌
#  - Depends(get_db)：获取数据库连接
async def get_authenticated_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db)
) -> User:
    """验证 JWT、账号状态与 token_version，但不处理首次改密业务门禁。"""
    token = credentials.credentials
    payload = decode_token(token)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无效的认证令牌",
            headers={"WWW-Authenticate": "Bearer"},
        )
    user_id = payload.get("sub")
    if user_id is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="令牌格式错误")
    user = db.query(User).filter(User.id == int(user_id)).first()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在")
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="账号已被禁用")
    if int(payload.get("tv") or 0) != int(user.token_version or 0):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=SESSION_REVOKED_DETAIL,
            headers={"WWW-Authenticate": "Bearer"},
        )
    from app.models.user_agent_access import UserAgentAccess
    access_rows = db.query(UserAgentAccess).filter(UserAgentAccess.user_id == user.id).all()
    user.agent_permissions = [str(a.agent_id) for a in access_rows]
    request.state.audit_actor_id = user.id
    return user


async def get_current_user(
    current_user: User = Depends(get_authenticated_user),
) -> User:
    """业务接口统一入口：首次登录未改密时只允许访问改密专用接口。"""
    if bool(current_user.must_change_password):
        from app.core.password_policy import PASSWORD_CHANGE_REQUIRED_DETAIL
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=PASSWORD_CHANGE_REQUIRED_DETAIL)
    return current_user


async def get_current_user_for_password_change(
    current_user: User = Depends(get_authenticated_user),
) -> User:
    """仅供 /auth/me、/users/me/password 使用，避免首次改密流程自锁。"""
    return current_user

async def get_user_from_query_token(
    token: str,
    db: Session = Depends(get_db),
) -> User:
    """用于浏览器新标签页直接打开的接口（如文件在线预览）。
    浏览器无法在 window.open 中附加 Authorization header，因此通过 ?token=xxx 传 JWT。"""
    payload = decode_token(token)
    if payload is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="无效的认证令牌")
    user_id = payload.get("sub")
    if user_id is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="令牌格式错误")
    user = db.query(User).filter(User.id == int(user_id)).first()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在")
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="账号已被禁用")
    if int(payload.get("tv") or 0) != int(user.token_version or 0):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=SESSION_REVOKED_DETAIL)
    from app.models.user_agent_access import UserAgentAccess
    access_rows = db.query(UserAgentAccess).filter(UserAgentAccess.user_id == user.id).all()
    user.agent_permissions = [str(a.agent_id) for a in access_rows]
    return user

# FastAPI 会自动按依赖关系从内到外依次执行：先提取令牌 → 再验证身份 → 最后检查是否管理员
# 高级权限的依赖注入验证
async def require_admin(current_user: User = Depends(get_current_user)) -> User:
    """要求管理员权限"""
    if current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="需要管理员权限"
        )
    return current_user


def has_perm(user: User, perm: str, db: Session | None = None) -> bool:
    """操作级权限判断：已存任一权限码是请求码的祖先或相等即拥有（节点码 = 整棵子树）。
    例：存 "file_all" 覆盖 "file_all.manage.rename"；存 "file_all.manage" 覆盖其下三个操作。
    admin 天然拥有全部权限（判定逻辑全项目只在这一处）。"""
    from app.services.organization_access import has_permission
    return has_permission(user, perm, db=db)


def has_module_perm(user: User, module: str, db: Session | None = None) -> bool:
    """模块级判断：在该模块子树内拥有任意权限即通过。
    用于菜单/页面显隐与列表类接口（能管理任何一项就需要看到列表）。"""
    from app.services.organization_access import has_module_permission
    return has_module_permission(user, module, db=db)


def require_perm(perm: str):
    """细粒度权限依赖工厂：用法 Depends(require_perm("mcp_manage.create"))，支持任意层级路径码。
    admin 直接放行；其他角色需被分配了对应权限，否则 403。"""
    async def checker(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> User:
        if not has_perm(current_user, perm, db=db):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="无此操作权限"
            )
        return current_user
    return checker


def require_module_perm(module: str):
    """模块级权限依赖工厂：用法 Depends(require_module_perm("agent_manage"))。
    模块子树内有任意权限即放行（列表类接口用）。"""
    async def checker(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> User:
        if not has_module_perm(current_user, module, db=db):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="无此操作权限"
            )
        return current_user
    return checker
