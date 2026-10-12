from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey, ForeignKey, ForeignKey, ForeignKey
from sqlalchemy.sql import func
from app.database import Base

class Skill(Base):
    __tablename__ = "skills"
    
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), unique=True, nullable=False)  # skill目录名
    display_name = Column(String(200), nullable=False)  # 显示名称（ 给用户看的友好名称）
    description = Column(String(500), nullable=True) # 前端展示在技能列表里，帮助用户了解这个技能能干什么
    required_role = Column(String(20), default="viewer")  # viewer/operator/admin
    is_active = Column(Boolean, default=1)
    # 内置技能标志：由技能所在目录决定（sync_from_filesystem 维护）
    # builtin_dialog=1 → skills_dialog/ 下（工具助手原生加载）
    # builtin_task=1   → skills_task/ 下（数据分析师原生加载）
    # 两者都为 0 → skills/ 下的可选技能（用户可勾选、广场可见）
    builtin_dialog = Column(Boolean, default=0)
    builtin_task = Column(Boolean, default=0)
    # 广场/聊天可见性（管理员态，不由文件系统同步维护）：
    # True=对未授权的非管理员隐藏（技能广场不可见；按决策 B 聊天也不可选）
    is_hidden = Column(Boolean, nullable=False, default=False, server_default="false")
    # 所属技能包（skills/ 下的一级目录，其下层存在 SKILL.md 即成包）；NULL=独立技能
    package_id = Column(Integer, nullable=True)
    # 磁盘身份：相对所属技能根目录的路径（POSIX 风格，如 "agent_package/skills_toby/interview-guide"）。
    # 同步/恢复/彻底删除一律按它定位目录，不再用目录名在多个根里递归猜（同名目录会误删别的技能）。
    # NULL=历史遗留行尚未回填，同步会按目录名尝试回填一次。
    rel_path = Column(String(500), nullable=True, index=True, unique=True)
    # 目录缺失标记：True=库里有记录但磁盘上找不到目录（由同步置位/复位）。
    # 用来区分"同步因文件缺失停用"与"管理员主动软删除"：只有 dir_missing=True 的行
    # 才会在目录重新出现时自动恢复，管理员的软删除不会被同步冲掉。
    dir_missing = Column(Boolean, nullable=False, default=False)
    # 归属锁定：True=管理员手动切换过归属（可选 <-> 工具助手内置 <-> 数据分析师内置），
    # 同步时不再按目录位置覆盖 builtin_dialog / builtin_task（见 skill_service.change_scope）。
    # 没有它，一次"刷新技能"就会把手动内置的结果打回原形。
    scope_locked = Column(Boolean, nullable=False, default=False)
    # 中文展示信息（由大模型自动生成，非直译：精简中文名 + 精炼中文描述）
    # 为空时前端回退展示 display_name / description 原文
    display_name_zh = Column(String(200), nullable=True)
    description_zh = Column(String(500), nullable=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
