from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.sql import func
from app.database import Base


class UserSkillAccess(Base):
    """用户技能授权：一条记录 = "用户 X 被授权 target"。

    target_type='skill'   → target_id 为 skills.id（单技能授权）
    target_type='package' → target_id 为 skill_packages.id（整包授权，
                            包内后续新增的成员技能自动生效）

    授权语义：覆盖 required_role 与广场隐藏标记——被授权用户始终可见且可用。
    管理员天然全量可见可用，不写授权记录。
    """
    __tablename__ = "user_skill_access"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    target_type = Column(String(10), nullable=False)  # skill / package
    target_id = Column(Integer, nullable=False)
    granted_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("user_id", "target_type", "target_id", name="uq_user_skill_target"),
        {"sqlite_autoincrement": True},
    )
