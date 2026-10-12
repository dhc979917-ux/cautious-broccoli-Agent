from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey, ForeignKey, ForeignKey, ForeignKey
from sqlalchemy.sql import func
from app.database import Base


class SkillPackage(Base):
    """技能包：skills/ 下的一级目录，其严格下层存在 SKILL.md 即识别为一个包。

    包身份以 rel_path（相对可选技能根目录的路径）为准，目录名只是展示用的人类可读标识；
    包内 README.md 平台不读不写（有就原样留在磁盘上），包名与包描述由 SKILL.md 生成。

    包内技能在 skills 表用 package_id 关联；包的软删除会整体隐藏成员技能
    （用户端不可见），硬删除级联删除全部成员技能（DB + 磁盘）。
    """
    __tablename__ = "skill_packages"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), unique=True, nullable=False)   # 包目录名（唯一标识）
    display_name = Column(String(200), nullable=False)        # 短名（如"阿里工具包"，LLM 生成，可编辑）
    description = Column(String(500), nullable=True)          # ~50 字总结（由 SKILL.md 素材生成，可编辑）
    # 包级权限：设置后成员技能统一沿用；NULL=不设置，成员技能各自保留独立权限
    required_role = Column(String(20), nullable=True)
    is_active = Column(Boolean, default=1)
    # 包级广场隐藏（管理员态）：True=整包成员对未授权的非管理员隐藏
    is_hidden = Column(Boolean, nullable=False, default=False, server_default="false")
    # 磁盘身份：相对可选技能根目录（skills/）的路径，一级包即目录名本身。
    # 恢复/彻底删除按它定位目录，找不到时只清 DB 记录，绝不再按目录名递归猜路径。
    rel_path = Column(String(500), nullable=True, index=True, unique=True)
    # True=磁盘上找不到包目录（同步置位）；恢复接口据此拒绝，避免"恢复后又被同步打回"
    dir_missing = Column(Boolean, nullable=False, default=False)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
