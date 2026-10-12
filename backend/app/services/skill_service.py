from sqlalchemy.orm import Session
from sqlalchemy import or_
from typing import Dict, List, Optional, Tuple
from app.models.skill import Skill
from app.models.skill_package import SkillPackage
from app.core.roles import ROLE_LEVELS
from app.config import settings
from app.services.model_gateway import endpoint, headers as gateway_headers
import os
import queue
import shutil
import threading
import time
import frontmatter
from contextlib import contextmanager
from dataclasses import dataclass, field


# ==================== 并发控制：技能变更串行化 ====================
# 同步 / 上传落盘 / 恢复 / 彻底删除共用一把锁：两次同步交叉提交会互相覆盖
# （并发"上传 + 刷新"曾留下半更新的包记录）。用 RLock 是为了让上传作业在
# 持锁期间还能调用 sync_from_filesystem（同线程重入），不同线程则直接 409。
_SKILL_LOCK = threading.RLock()


class SkillBusyError(RuntimeError):
    """已有技能变更在进行（同步/上传/删除）；API 层转 409。"""


class SkillDirMissingError(RuntimeError):
    """库里有记录但磁盘目录不在：恢复类操作转 409，提示重新上传，而不是静默翻牌被同步打回。"""

    def __init__(self, name: str, rel_path: Optional[str] = None):
        self.name = name
        self.rel_path = rel_path
        super().__init__(
            f"磁盘上找不到 '{name}' 的目录（{rel_path or '路径未记录'}），无法恢复；请重新上传技能包"
        )


class SkillDeleteConflictError(RuntimeError):
    """目标目录还是其它已注册技能/技能包的容器；彻底删除转 409，避免连带删掉整包。"""


class SkillScopeError(RuntimeError):
    """归属切换（可选 <-> 内置）被拒绝：包成员、重名、目录缺失、嵌套技能等；API 层转 400。"""


@contextmanager
def skill_mutation_lock(timeout: float = 0.0, poll: float = 0.2):
    """获取技能变更锁。

    timeout=0（默认）：不等待，被占用即抛 SkillBusyError —— 请求路径用，API 层转 409，
    让用户知道"稍后重试"而不是排队卡住。
    timeout>0：轮询等待到多这么久 —— 后台上传作业用，避免与一次刷新撞锁就整个作业失败。
    """
    deadline = time.monotonic() + max(0.0, timeout)
    got = _SKILL_LOCK.acquire(blocking=False)
    while not got and time.monotonic() < deadline:
        time.sleep(poll)
        got = _SKILL_LOCK.acquire(blocking=False)
    if not got:
        raise SkillBusyError("已有技能同步/上传/删除正在进行，请稍后重试")
    try:
        yield
    finally:
        _SKILL_LOCK.release()


# ==================== 磁盘扫描：以相对路径为身份 ====================
ROOT_OPTIONAL = "optional"
ROOT_DIALOG = "builtin_dialog"
ROOT_TASK = "builtin_task"

# 根目录标识 → rel_path 的第一段（相对 app/agents/），保证跨根唯一：
# skills_dialog/docx 与 skills_task/docx 是两个不同目录，不能共用一个身份。
_ROOT_SEGMENT = {ROOT_OPTIONAL: "skills", ROOT_DIALOG: "skills_dialog", ROOT_TASK: "skills_task"}
# 同名目录归并成一条技能行时的取舍优先级（可选根优先，与历史行为一致）
_ROOT_PRIORITY = {ROOT_OPTIONAL: 0, ROOT_DIALOG: 1, ROOT_TASK: 2}

# 归属（scope）：管理端在 可选 / 工具助手内置 / 数据分析师内置 之间切换技能时的稳定标识。
# 与根类型一一对应；落库仍复用 builtin_dialog / builtin_task 两个布尔列 + scope_locked 锁，
# 不新增"归属"状态列，避免同一件事有两处真相。
SCOPE_OPTIONAL = "optional"
SCOPE_DIALOG = "dialog"
SCOPE_TASK = "task"
SCOPE_BY_ROOT = {ROOT_OPTIONAL: SCOPE_OPTIONAL, ROOT_DIALOG: SCOPE_DIALOG, ROOT_TASK: SCOPE_TASK}
ROOT_BY_SCOPE = {v: k for k, v in SCOPE_BY_ROOT.items()}
SCOPE_LABELS = {
    SCOPE_OPTIONAL: "可选技能",
    SCOPE_DIALOG: "工具助手内置技能",
    SCOPE_TASK: "数据分析师内置技能",
}


def scope_of_root(root_kind: Optional[str]) -> str:
    """根类型 -> 归属标识（None/未知一律按可选处理）。"""
    return SCOPE_BY_ROOT.get(root_kind or ROOT_OPTIONAL, SCOPE_OPTIONAL)


def scope_of_flags(skill) -> str:
    """库里的内置标志 -> 归属标识。两个标志同时置位（同名共用技能）时以 dialog 为准，
    与扫描归并的取舍优先级保持一致。"""
    if getattr(skill, "builtin_dialog", False):
        return SCOPE_DIALOG
    if getattr(skill, "builtin_task", False):
        return SCOPE_TASK
    return SCOPE_OPTIONAL


def _prune_hidden(dirs: list) -> None:
    """跳过点开头的目录：上传作业先写 skills/.staging-<job>/ 再原子改名，
    半成品既不能被同步注册，也不能被 Agent 装载。"""
    dirs[:] = [d for d in dirs if not d.startswith(".")]


@dataclass
class DiskSkill:
    """磁盘上的一个技能目录（含 SKILL.md）。"""
    name: str                 # 目录名，= Skill.name
    rel_path: str             # 相对 app/agents/ 的路径（含根段），全局唯一，落库身份
    rel_in_root: str          # 相对所属根目录的路径（判包用）
    abs_path: str
    root_kind: str            # optional / builtin_dialog / builtin_task
    meta: dict = field(default_factory=dict)


def scan_skill_dirs(roots: List[Tuple[str, Optional[str]]]) -> List[DiskSkill]:
    """扫描各技能根目录，返回全部含 SKILL.md 的目录（不做同名合并）。"""
    out: List[DiskSkill] = []
    for root_dir, flag in roots:
        if not os.path.isdir(root_dir):
            continue
        kind = flag or ROOT_OPTIONAL
        seg = _ROOT_SEGMENT.get(kind, "skills")
        for root, dirs, files in os.walk(root_dir):
            _prune_hidden(dirs)
            if "SKILL.md" not in files:
                continue
            abs_path = os.path.normpath(root)
            rel_in_root = os.path.relpath(abs_path, root_dir).replace(os.sep, "/")
            try:
                meta = frontmatter.load(os.path.join(abs_path, "SKILL.md")).metadata
            except Exception as e:
                print(f"[SkillService] SKILL.md 解析失败，跳过 {abs_path}: {e}")
                continue
            out.append(DiskSkill(
                name=os.path.basename(abs_path),
                rel_path=f"{seg}/{rel_in_root}" if rel_in_root != "." else seg,
                rel_in_root=rel_in_root,
                abs_path=abs_path,
                root_kind=kind,
                meta=meta if isinstance(meta, dict) else {},
            ))
    return out


def index_skills_by_name(entries: List[DiskSkill]) -> Tuple[Dict[str, DiskSkill], List[str]]:
    """按目录名归并（Skill.name 有唯一约束）：可选根优先，其次按 rel_path 字典序。

    落选的同名目录不再被静默丢弃，而是回一条告警，便于发现"同名技能互相遮蔽"。
    """
    chosen: Dict[str, DiskSkill] = {}
    shadowed: List[str] = []
    for e in sorted(entries, key=lambda x: (_ROOT_PRIORITY.get(x.root_kind, 9), x.rel_path)):
        cur = chosen.get(e.name)
        if cur is None:
            chosen[e.name] = e
        else:
            shadowed.append(f"{e.rel_path}（同名技能已取 {cur.rel_path}）")
    return chosen, shadowed


def detect_packages(entries: List[DiskSkill]) -> Dict[str, str]:
    """结构判包（完全不读 README）：可选根下的一级目录，只要其严格下层还有 SKILL.md，
    该一级目录就是一个技能包，其下全部技能（含一级目录自身的 SKILL.md）都是成员；
    一级目录只有自己一个 SKILL.md 时是独立技能。返回 {技能 rel_path: 包 rel_path}。
    """
    optional = [e for e in entries if e.root_kind == ROOT_OPTIONAL]
    tops_with_children = {e.rel_in_root.split("/")[0] for e in optional if "/" in e.rel_in_root}
    seg = _ROOT_SEGMENT[ROOT_OPTIONAL]
    return {
        e.rel_path: f"{seg}/{e.rel_in_root.split('/')[0]}"
        for e in optional
        if e.rel_in_root.split("/")[0] in tops_with_children
    }


# ==================== 后台富化：包摘要 + 中文展示名（唯一允许调模型的地方） ====================
# 同步流程只做本地文件与数据库操作；所有 LLM 调用（单批超时 120s）都排进这个队列，
# 由单个 daemon 线程串行消费：既不冻结事件循环，也不会并发打爆模型配额。
_ENRICH_QUEUE: "queue.Queue" = queue.Queue()
_ENRICH_PENDING: set = set()
_ENRICH_LOCK = threading.Lock()
_ENRICH_THREAD: Optional[threading.Thread] = None


def _invalidate_skill_cache() -> None:
    """技能展示信息落库后失效列表缓存，否则前端要等缓存过期才能看到中文名。"""
    try:
        from app.core.cache import cache_delete_pattern
        cache_delete_pattern("skills:*")
    except Exception:
        pass


def spawn_skill_enrichment(package_ids: Optional[List[int]] = None, translate: bool = True) -> int:
    """把"包摘要生成 / 技能中文展示名生成"排入后台队列，返回入队任务数。

    幂等：同一任务在消费完成前重复触发会被合并（上传与刷新并发不会重复调模型）。
    """
    global _ENRICH_THREAD
    tasks: List[Tuple[str, Optional[int]]] = [("package_meta", pid) for pid in (package_ids or [])]
    if translate:
        tasks.append(("translate", None))
    queued = 0
    with _ENRICH_LOCK:
        for t in tasks:
            if t in _ENRICH_PENDING:
                continue
            _ENRICH_PENDING.add(t)
            _ENRICH_QUEUE.put(t)
            queued += 1
        if queued and (_ENRICH_THREAD is None or not _ENRICH_THREAD.is_alive()):
            _ENRICH_THREAD = threading.Thread(
                target=_enrichment_worker, daemon=True, name="skill-enrich")
            _ENRICH_THREAD.start()
    if tasks and queued == 0:
        print("[SkillService] 后台富化任务已在进行，跳过本次触发")
    return queued


def spawn_background_translate() -> None:
    """兼容旧调用点：等价于只排队"中文展示名生成"。"""
    spawn_skill_enrichment(translate=True)


def sync_in_new_session() -> dict:
    """在独立数据库会话里跑一次同步，供 asyncio.to_thread / 后台任务调用。

    SQLAlchemy 会话非线程安全：不能把请求线程或启动流程的 db 传进线程池，
    统一在这里开短会话，调用方只需 await asyncio.to_thread(sync_in_new_session)。
    """
    from app.database import SessionLocal
    with SessionLocal() as tdb:
        return SkillService(tdb).sync_from_filesystem()


def change_scope_in_new_session(skill_id: int, scope: str) -> dict:
    """独立会话里切换技能归属，供 asyncio.to_thread 调用。

    与 sync_in_new_session 同一个理由：SQLAlchemy 会话非线程安全，不能把请求线程的
    db 传进线程池。返回切换后的关键字段，API 层再用请求会话读完整一行回给前端。
    """
    from app.database import SessionLocal
    with SessionLocal() as tdb:
        skill = SkillService(tdb).change_scope(skill_id, scope)
        return {
            "id": skill.id,
            "name": skill.name,
            "scope": scope_of_flags(skill),
            "rel_path": skill.rel_path,
        }


def _enrichment_worker() -> None:
    """队列消费线程：空闲即退出（下次触发重新起），任务失败不影响其它任务。"""
    global _ENRICH_THREAD
    from app.database import SessionLocal

    while True:
        try:
            kind, arg = _ENRICH_QUEUE.get(timeout=2.0)
        except queue.Empty:
            with _ENRICH_LOCK:
                if _ENRICH_QUEUE.empty():
                    _ENRICH_THREAD = None
                    return
            continue
        db = SessionLocal()
        try:
            svc = SkillService(db)
            if kind == "package_meta":
                svc.generate_package_meta(arg)
            elif kind == "translate":
                stats = svc.translate_pending_skills()
                if stats.get("translated"):
                    print(f"[SkillService] 已为 {stats['translated']} 个技能生成中文展示信息")
            _invalidate_skill_cache()
        except Exception as e:
            print(f"[SkillService] 后台富化任务失败（{kind} {arg}，不影响已落盘的技能）: {e}")
        finally:
            db.close()
            # 任务真正结束后才移出 pending：执行期间的重复触发按"已在进行"合并
            with _ENRICH_LOCK:
                _ENRICH_PENDING.discard((kind, arg))



'''技能的管理：按角色过滤可用技能、查全部技能、注册新技能'''

# ROLE_LEVELS 统一在 app.core.roles 定义，这里 re-export 兼容旧引用
__all__ = ["SkillService", "ROLE_LEVELS"]

class SkillService:
    def __init__(self, db: Session):
        self.db = db
    
    def get_skills_by_role(self, user_role: str) -> List[Skill]:
        """根据用户角色获取可用skill列表"""
        # 用户角色转数字(用user_role去对应字典中找数字，找不到就返回0（无任何权限）)
        user_level = ROLE_LEVELS.get(user_role, 0)
        
        # 查询所有active的skills
        # 仅返回可选技能：内置技能（builtin_dialog/builtin_task=1）对用户端不可见，
        # 聊天选择与技能广场都走这里，天然过滤掉内置技能；
        # 被软删除的包，其成员技能也整体隐藏
        q = self.db.query(Skill).filter(
            Skill.is_active == True,
            Skill.builtin_dialog == False,
            Skill.builtin_task == False,
        )
        inactive_pkgs = self._inactive_package_ids()
        if inactive_pkgs:
            q = q.filter(or_(Skill.package_id.is_(None), ~Skill.package_id.in_(inactive_pkgs)))
        all_skills = q.all()
        
        # 按权限过滤（逐个比较：用户等级 ≥ 技能要求等级 → 加入结果）
        available = []
        for skill in all_skills:
            skill_level = ROLE_LEVELS.get(skill.required_role, 0)
            if user_level >= skill_level:
                available.append(skill)
        
        return available

    # ==================== 广场可见性 + 用户授权（决策 B）====================
    # 规则（唯一权威实现，广场/聊天/定时任务共用）：
    #   visible = admin 或 granted 或 not hidden
    #   usable  = admin 或 granted 或 (not hidden 且 角色达标)
    # 即：隐藏技能只有管理员和被授权用户可见可用；公开技能保持原有角色门槛。

    def get_user_skill_grants(self, user_id: int):
        """返回 (granted_skill_ids, granted_package_ids)。"""
        from app.models.user_skill_access import UserSkillAccess
        rows = self.db.query(UserSkillAccess).filter(
            UserSkillAccess.user_id == user_id
        ).all()
        granted_skills = {r.target_id for r in rows if r.target_type == "skill"}
        granted_packages = {r.target_id for r in rows if r.target_type == "package"}
        return granted_skills, granted_packages

    def get_hidden_package_ids(self) -> set:
        """is_hidden=True 的技能包 id 集合（含已停用包，成员过滤交给上游查询）。"""
        rows = self.db.query(SkillPackage.id).filter(SkillPackage.is_hidden == True).all()  # noqa: E712
        return {r[0] for r in rows}

    @staticmethod
    def evaluate_access(*, is_admin: bool, granted: bool, hidden: bool,
                        user_role: str, required_role: str) -> bool:
        """决策 B：隐藏技能仅 admin/granted 可用；公开技能按角色门槛，授权可覆盖。"""
        if is_admin or granted:
            return True
        if hidden:
            return False
        return ROLE_LEVELS.get(user_role, 0) >= ROLE_LEVELS.get(required_role, 0)

    def get_usable_skills_for_user(self, user) -> List[Skill]:
        """聊天/定时任务可用技能（含授权覆盖；隐藏技能仅 admin/granted）。"""
        skills = self.get_all_skills()
        if settings.GOVERNANCE_RESOURCE_VERSIONING_ENABLED:
            from app.models.skill_governance import SkillPublication
            published_ids = {row[0] for row in self.db.query(SkillPublication.skill_id)}
            skills = [skill for skill in skills if skill.id in published_ids]
        is_admin = getattr(user, "role", None) == "admin"
        from app.services.skill_hub.runtime import usable
        if is_admin:
            return usable(self.db, user, skills)
        granted_skills, granted_packages = self.get_user_skill_grants(user.id)
        hidden_pkgs = self.get_hidden_package_ids()
        result = []
        for skill in skills:
            granted = skill.id in granted_skills or skill.package_id in granted_packages
            hidden = bool(skill.is_hidden) or skill.package_id in hidden_pkgs
            if self.evaluate_access(is_admin=is_admin, granted=granted, hidden=hidden,
                                    user_role=getattr(user, "role", "viewer"),
                                    required_role=skill.required_role):
                result.append(skill)
        return usable(self.db, user, result)

    def filter_visible_skills_for_user(self, base_items: list, user) -> list:
        """对 GET /skills/ 的基础缓存条目做可见性过滤并计算 has_access。

        base_items 每项需含 is_hidden / package_is_hidden / required_role；
        返回可见条目（dict 副本）并写入 has_access。不可见（隐藏且未授权非管理员）直接剔除。
        """
        is_admin = getattr(user, "role", None) == "admin"
        current_skills = {skill.id: skill for skill in self.get_all_skills()}
        packages = {package.id: package for package in self.get_all_packages_include_deleted()}
        granted_skills, granted_packages = self.get_user_skill_grants(user.id)
        published_ids = None
        if settings.GOVERNANCE_RESOURCE_VERSIONING_ENABLED:
            from app.models.skill_governance import SkillPublication
            published_ids = {row[0] for row in self.db.query(SkillPublication.skill_id)}
        result = []
        from app.services.skill_hub import access as hub_access
        personal_ids = set(hub_access.managed_map(self.db))
        for item in base_items:
            if item.get("id") in personal_ids:
                continue
            current = current_skills.get(item.get('id'))
            if current is None:
                continue
            if published_ids is not None and item.get("id") not in published_ids:
                continue
            package = packages.get(current.package_id)
            hidden = bool(current.is_hidden) or bool(package and package.is_hidden)
            granted = (item.get("id") in granted_skills
                       or current.package_id in granted_packages)
            if hidden and not (is_admin or granted):
                continue
            entry = dict(item)
            entry.update(is_hidden=bool(current.is_hidden), required_role=current.required_role,
                         package_id=current.package_id, package_is_hidden=bool(package and package.is_hidden))
            entry["has_access"] = self.evaluate_access(
                is_admin=is_admin, granted=granted, hidden=hidden,
                user_role=getattr(user, "role", "viewer"),
                required_role=current.required_role,
            )
            result.append(entry)
        from app.services.skill_hub.runtime import decorate
        return decorate(self.db, user, result)
    
    def get_all_skills(self) -> List[Skill]:
        """获取所有skill（管理员用）"""
        # 按添加时间升序（同一时刻添加的按 id 兜底），前端序号随此顺序自适应
        q = self.db.query(Skill).filter(
            Skill.is_active == True,
            Skill.builtin_dialog == False,
            Skill.builtin_task == False,
        )
        inactive_pkgs = self._inactive_package_ids()
        if inactive_pkgs:
            q = q.filter(or_(Skill.package_id.is_(None), ~Skill.package_id.in_(inactive_pkgs)))
        return q.order_by(Skill.created_at, Skill.id).all()
    
    def _create_skill_directory(self, name, display_name, description, required_role) -> str:
        """Auto-create skill directory structure；返回该目录的 rel_path（已存在则直接返回）"""
        skills_dir = os.path.join(self._skills_root(), name)
        rel_path = f"{_ROOT_SEGMENT[ROOT_OPTIONAL]}/{name}"
        if os.path.exists(skills_dir):
            return rel_path
        os.makedirs(skills_dir)
        os.makedirs(os.path.join(skills_dir, "scripts"))
        
        # Create SKILL.md
        skill_md = f"""---\nname: {name}\ndescription: {description or display_name}\nrequired_role: {required_role}\n---\n\n# {display_name}\n\n## Usage\n```bash\npython scripts/main.py [args]\n```\n"""
        with open(os.path.join(skills_dir, "SKILL.md"), "w", encoding="utf-8") as f:
            f.write(skill_md)
        
        # Create main.py
        main_py = """import sys\n\ndef main():\n    if len(sys.argv) < 2:\n        print("Usage: python main.py [args]")\n        return\n    result = f"Result: {sys.argv[1]}"\n    print(result)\n\nif __name__ == \"__main__\":\n    main()\n"""
        with open(os.path.join(skills_dir, "scripts", "main.py"), "w", encoding="utf-8") as f:
            f.write(main_py)
        return rel_path
    
    def register_skill(self, name: str, display_name: str, description: str,
                       required_role: str, is_hidden: bool = False) -> Skill:
        """注册一个新skill到数据库"""
        existing = self.db.query(Skill).filter(Skill.name == name).first()
        if existing:
            # 更新已有记录（存在就更新，不存在就新建）
            existing.display_name = display_name
            existing.description = description
            existing.required_role = required_role
            existing.is_hidden = bool(is_hidden)
            if not existing.rel_path:
                existing.rel_path = f"{_ROOT_SEGMENT[ROOT_OPTIONAL]}/{name}"
            self.db.commit()
            self.db.refresh(existing)
            return existing
        
        skill = Skill(
            name=name,
            display_name=display_name,
            description=description,
                required_role=required_role,
                is_hidden=bool(is_hidden),
            rel_path=f"{_ROOT_SEGMENT[ROOT_OPTIONAL]}/{name}",
        )
        self.db.add(skill)
        self.db.commit()
        self.db.refresh(skill)
        
        # Auto-create directory
        self._create_skill_directory(name, display_name, description, required_role)
        return skill

    def get_all_skills_include_deleted(self) -> List[Skill]:
        """获取所有Skill（包括已删除的，管理员用）"""
        from app.services.skill_hub.access import system_only
        return system_only(self.db.query(Skill), self.db, Skill).order_by(Skill.created_at, Skill.id).all()

    def delete_skill(self, skill_id: int):
        """软删除Skill（管理员意图）：dir_missing 置 False，同步不会把它重新激活。"""
        skill = self.db.query(Skill).filter(Skill.id == skill_id).first()
        if not skill:
            raise ValueError("Skill不存在")
        with skill_mutation_lock():
            skill.is_active = 0
            skill.dir_missing = False
            self.db.commit()

    def restore_skill(self, skill_id: int):
        """恢复Skill：先确认磁盘目录还在，否则抛 SkillDirMissingError（API 转 409）。

        目录不在时盲目翻牌没有意义——下一次同步会按"磁盘是唯一事实来源"再打回，
        管理面板就会看到"恢复了又自动被删"的怪现象。
        """
        skill = self.db.query(Skill).filter(Skill.id == skill_id).first()
        if not skill:
            raise ValueError("Skill不存在")
        with skill_mutation_lock():
            resolved, _candidates = self._resolve_dir(skill.rel_path, skill.name, require_skill_md=True)
            if not resolved:
                raise SkillDirMissingError(skill.name, skill.rel_path)
            skill.rel_path = self._rel_from_abs(resolved) or skill.rel_path
            skill.is_active = 1
            skill.dir_missing = False
            self.db.commit()

    def get_skill_by_id(self, skill_id: int) -> Skill:
        """根据ID获取Skill"""
        skill = self.db.query(Skill).filter(Skill.id == skill_id).first()
        if not skill:
            raise ValueError("Skill不存在")
        return skill

    def update_skill(self, skill_id: int, display_name: str = None, description: str = None,
                     required_role: str = None, is_hidden: bool = None):
        """更新Skill信息"""
        skill = self.get_skill_by_id(skill_id)
        if display_name is not None:
            skill.display_name = display_name
        if description is not None:
            skill.description = description
        if required_role is not None:
            skill.required_role = required_role
            # 同步回写 SKILL.md frontmatter，保持文件与 DB 一致（DB 仍是唯一权威）
            self._write_role_to_skill_md(skill.name, required_role, skill.rel_path)
        if is_hidden is not None:
            skill.is_hidden = bool(is_hidden)
        self.db.commit()
        self.db.refresh(skill)
        return skill

    # ==================== 归属切换（可选 <-> 内置） ====================

    @classmethod
    def _root_dir(cls, root_kind: str) -> str:
        """按根类型取根目录绝对路径（与 _skills_roots 共用同一份段名映射，不会走偏）。"""
        return os.path.join(cls._agents_dir(), _ROOT_SEGMENT[root_kind])

    def change_scope(self, skill_id: int, scope: str) -> Skill:
        """把技能在 可选 / 工具助手内置 / 数据分析师内置 之间移动（剪切语义）。

        实现是"移动目录 + 重扫同步 + 锁定归属"：
        - 目录位置仍是内置装载的事实来源（architectures/base.py 按根目录装载内置技能），
          因此装载链路一行都不用改；
        - scope_locked 让后续同步不再按目录位置覆盖管理员的决定（刷新技能不会打回原形）；
        - 三个根都在 app/agents/ 下：容器部署时 skills_dialog / skills_task 必须挂持久卷
          （见 docker-compose.yml），否则重建容器后移动结果会随镜像层一起丢失。

        幂等：目标归属与当前一致时直接返回，不动磁盘。
        失败回滚：同步异常时把目录移回原位，磁盘与库不会各说一套。

        包成员：允许切换，等价于"该成员脱离技能包"——目录被移到目标根的一级，
        重扫后 package_id 自然置空，包内其余成员不受影响。
        """
        if scope not in ROOT_BY_SCOPE:
            raise SkillScopeError(f"归属只能是 {'/'.join(ROOT_BY_SCOPE)}，收到 '{scope}'")

        # 锁是可重入的（RLock）：持锁期间再调 sync_from_filesystem 属于同线程重入，安全
        with skill_mutation_lock():
            skill = self.get_skill_by_id(skill_id)
            current = scope_of_flags(skill)
            if current == scope:
                return skill
            if not skill.is_active:
                # 软删除的技能目录可能已被移走/标记缺失，先恢复再切归属，避免状态叠加
                raise SkillScopeError(f"'{skill.name}' 处于删除状态，请先恢复后再切换归属")

            src = self._abs_from_rel(skill.rel_path)
            if not src or not os.path.isdir(src) or not os.path.isfile(os.path.join(src, "SKILL.md")):
                raise SkillScopeError(
                    f"磁盘上找不到 '{skill.name}' 的技能目录（{skill.rel_path or '路径未记录'}），无法切换归属"
                )
            nested = self._nested_skill_dirs(src)
            if nested:
                # 目录本身是技能包（下面还嵌着别的技能）：整体移走会把包拆散，
                # 也让"包"这个概念在内置根里失去意义（detect_packages 只扫可选根）。
                # 需要内置包里的技能时，展开包对单个成员操作即可。
                raise SkillScopeError(
                    f"'{skill.name}' 目录下还有 {len(nested)} 个嵌套技能，整体移动会改变包结构，已拒绝"
                )

            # 同名目录同时存在于多个根（如 docx/pdf 这类"工具助手与数据分析师共用"的内置技能）：
            # 只移动一份会留下"库里已取消内置、另一个根还有一份"的分裂状态，直接拒绝并说明。
            _resolved, candidates = self._resolve_dir(None, skill.name, require_skill_md=True)
            if len(candidates) > 1:
                raise SkillScopeError(
                    f"'{skill.name}' 在多个技能根下都有同名目录（{len(candidates)} 份），"
                    "切换归属会造成磁盘与库不一致；请先删掉多余的副本再操作"
                )

            target_root = self._root_dir(ROOT_BY_SCOPE[scope])
            os.makedirs(target_root, exist_ok=True)
            dst = os.path.join(target_root, os.path.basename(src))
            if os.path.exists(dst):
                raise SkillScopeError(
                    f"{SCOPE_LABELS[scope]}目录下已存在同名技能 '{os.path.basename(src)}'；"
                    "请先处理重名（软删除或改名）后再切换归属"
                )

            # 移动前先解锁：紧接着这次同步必须按新位置重写 rel_path 与内置标志，
            # 归属锁是用来挡住"之后的刷新/启动同步"的，不能挡住切换自身这一步
            was_locked = bool(skill.scope_locked)
            skill.scope_locked = False
            self.db.commit()

            shutil.move(src, dst)
            try:
                stats = self.sync_from_filesystem()
                # 同步中止（可选根缺失/扫不出技能）时不会落库，磁盘却已经移动：
                # 必须把目录移回原位，否则库里 rel_path 指向一个不存在的目录
                if stats.get("aborted"):
                    raise SkillScopeError(
                        f"技能同步已中止（{stats['aborted']}），本次归属切换已回滚；"
                        "请检查技能目录挂载后重试"
                    )
            except Exception:
                shutil.move(dst, src)
                # 目录已移回原位，锁也恢复原状，避免留下"半切换"状态
                try:
                    self.db.rollback()
                    self.get_skill_by_id(skill_id).scope_locked = was_locked
                    self.db.commit()
                except Exception as rollback_error:
                    print(f"[SkillService] 归属切换回滚时恢复锁定标志失败: {rollback_error}")
                raise

            # 播种抑制名单：移出的根记一笔（否则 entrypoint 会从镜像种子把它补回去），
            # 移入的根清一笔（技能回来了，名单里留着会让以后的播种一直跳过它）。
            # 放在同步成功之后：失败回滚时不会留下错误的抑制记录。
            self._update_seed_tracker(ROOT_BY_SCOPE[current], skill.name, suppress=True)
            self._update_seed_tracker(ROOT_BY_SCOPE[scope], skill.name, suppress=False)

            # 同步用的就是本会话：过期后重读，确保拿到重扫后的 rel_path
            self.db.expire_all()
            skill = self.get_skill_by_id(skill_id)
            skill.builtin_dialog = scope == SCOPE_DIALOG
            skill.builtin_task = scope == SCOPE_TASK
            skill.scope_locked = True
            skill.dir_missing = False
            self.db.commit()
            self.db.refresh(skill)
            _invalidate_skill_cache()
            print(f"[SkillService] 归属切换: '{skill.name}' {current} -> {scope}（{src} -> {dst}）")
            return skill

    def _write_role_to_skill_md(self, name: str, required_role: str, rel_path: Optional[str] = None) -> bool:
        """把 required_role 回写到 SKILL.md frontmatter（按行替换，不动其他内容）。

        文件不存在或无 frontmatter 时静默跳过（手动创建的技能可能没有文件）。
        """
        skill_dir, _candidates = self._resolve_dir(rel_path, name, require_skill_md=True)
        if not skill_dir:
            return False
        skill_md = os.path.join(skill_dir, "SKILL.md")
        try:
            with open(skill_md, "r", encoding="utf-8") as f:
                content = f.read()
            if not content.startswith("---"):
                return False
            end = content.find("---", 3)
            if end == -1:
                return False
            # 兼容 CRLF/LF：逐行去掉行尾 \r，重建统一用 \n
            fm_lines = [l.rstrip("\r") for l in content[3:end].strip("\r\n").split("\n")]
            replaced = False
            for i, line in enumerate(fm_lines):
                if line.strip().startswith("required_role:"):
                    fm_lines[i] = f"required_role: {required_role}"
                    replaced = True
                    break
            if not replaced:
                fm_lines.append(f"required_role: {required_role}")
            new_content = "---\n" + "\n".join(fm_lines) + "\n" + content[end:]
            with open(skill_md, "w", encoding="utf-8") as f:
                f.write(new_content)
            return True
        except Exception as e:
            print(f"[SkillService] 警告: 回写 SKILL.md 失败 ({name}): {e}")
            return False

    def hard_delete_skill(self, skill_id: int) -> dict:
        """彻底删除单个技能：DB 行必删，磁盘目录只在能唯一定位且不牵连其它技能时才删。

        三条安全线（历史上"按目录名在三个根里取第一个匹配"会删掉整包或内置技能）：
        1. 目录里还嵌着别的 SKILL.md（技能目录同时是包根，如 agent_package）→ 拒绝，提示改用包级删除；
        2. 按名字兜底时命中多个候选目录 → 拒绝，不猜；
        3. 目录已不在（僵尸记录）→ 只清 DB 行，返回 db_only=True。
        """
        skill = self.db.query(Skill).filter(Skill.id == skill_id).first()
        if not skill:
            raise ValueError("Skill不存在")
        with skill_mutation_lock():
            resolved, candidates = self._resolve_dir(skill.rel_path, skill.name, require_skill_md=True)
            if resolved is None and len(candidates) > 1:
                raise SkillDeleteConflictError(
                    f"'{skill.name}' 在磁盘上对应多个目录（{', '.join(candidates)}），"
                    "无法确定要删哪一个；请先修正记录的路径或改用技能包级删除"
                )
            nested = self._nested_skill_dirs(resolved) if resolved else []
            if nested:
                raise SkillDeleteConflictError(
                    f"'{skill.name}' 的目录里还包含 {len(nested)} 个其它技能"
                    f"（{', '.join(os.path.basename(p) for p in nested[:5])}），"
                    "它其实是技能包根目录；请改用技能包的彻底删除"
                )
            rel_path = skill.rel_path
            name = skill.name
            from app.services.resource_deletion import remove_skill_history
            snapshot_dirs = remove_skill_history(self.db, [skill.id], [skill.name])
            from app.models.user_skill_access import UserSkillAccess
            self.db.query(UserSkillAccess).filter(
                UserSkillAccess.target_type == "skill",
                UserSkillAccess.target_id == skill.id,
            ).delete(synchronize_session=False)
            self._record_builtin_deletion(skill)
            self.db.delete(skill)
            self.db.commit()
            for snapshot_dir in snapshot_dirs:
                shutil.rmtree(snapshot_dir)
            removed = False
            if resolved:
                shutil.rmtree(resolved, ignore_errors=True)
                removed = True
                print(f"[SkillService] 彻底删除技能 '{name}'（{rel_path or resolved}），目录已移除")
            else:
                print(f"[SkillService] 彻底删除技能 '{name}'：磁盘目录不存在，仅清理数据库记录")
            return {"name": name, "dir_removed": removed, "db_only": not removed}

    def _record_builtin_deletion(self, skill) -> None:
        """内置技能被彻底删除时记入播种抑制名单，杜绝容器重启后从镜像种子复活。"""
        if skill.builtin_dialog:
            self._update_seed_tracker(ROOT_DIALOG, skill.name, suppress=True)
        if skill.builtin_task:
            self._update_seed_tracker(ROOT_TASK, skill.name, suppress=True)
        if not skill.builtin_dialog and not skill.builtin_task and len((skill.rel_path or '').split('/')) == 2:
            self._update_seed_tracker(ROOT_OPTIONAL, skill.name, suppress=True)

    def _update_seed_tracker(self, root_kind: str, name: str, suppress: bool) -> None:
        """维护内置根的 .deleted_skills 播种抑制名单（docker-entrypoint.sh 据此跳过回灌）。

        suppress=True  记入名单：技能被彻底删除，或被管理员移出该根（取消内置）——
                       两种情况都不能再让镜像种子把它补回来；
        suppress=False 移出名单：技能又回到该根，名单里留着会让以后的播种一直跳过它。

        只处理内置根：可选根没有名单文件，它的回灌由 entrypoint 检查内置根来阻止
        （技能被"内置"后，可选根的镜像种子不再补种同名目录）。
        """
        if root_kind not in (ROOT_OPTIONAL, ROOT_DIALOG, ROOT_TASK):
            return
        seg = _ROOT_SEGMENT[root_kind]
        root_dir = os.path.join(self._agents_dir(), seg)
        tracker = os.path.join(root_dir, ".deleted_skills")
        try:
            existing = set()
            if os.path.isfile(tracker):
                with open(tracker, "r", encoding="utf-8") as f:
                    existing = {line.strip() for line in f if line.strip()}
            if suppress == (name in existing):
                return  # 名单已是目标状态，不必写盘
            if suppress:
                existing.add(name)
            else:
                existing.discard(name)
            os.makedirs(root_dir, exist_ok=True)
            # 整体重写而不是追加：文件内容就是名单本身，重复写入不会越积越长
            with open(tracker, "w", encoding="utf-8") as f:
                f.write("".join(sorted(item + "\n" for item in existing)))
            print(f"[SkillService] 播种抑制已{'记录' if suppress else '解除'}: {name}（{seg}/.deleted_skills）")
        except Exception as e:
            print(f"[SkillService] 维护播种抑制名单失败（{name}）: {e}")

    # ==================== 目录定位（以库里记录的 rel_path 为准，不再按目录名猜） ====================

    def _find_skill_directory(self, name: str) -> Optional[str]:
        """兼容旧调用：按目录名找技能目录；命中多个时返回 None（不猜）。"""
        resolved, _candidates = self._resolve_dir(None, name, require_skill_md=True)
        return resolved

    def _resolve_dir(self, rel_path: Optional[str], name: str,
                     require_skill_md: bool = True) -> Tuple[Optional[str], List[str]]:
        """定位磁盘目录：优先用库里记录的 rel_path，缺失时按目录名扫描兜底。

        返回 (唯一确定的目录, 全部候选)。候选多于一个时第一项为 None——调用方据此拒绝操作，
        宁可报错也不删错目录。
        """
        by_rel = self._abs_from_rel(rel_path)
        if by_rel and os.path.isdir(by_rel):
            return by_rel, [by_rel]
        candidates: List[str] = []
        for root_dir, _flag in self._skills_roots():
            if not os.path.isdir(root_dir):
                continue
            for root, dirs, files in os.walk(root_dir):
                _prune_hidden(dirs)
                p = os.path.normpath(root)
                if p == os.path.normpath(root_dir) or os.path.basename(p) != name:
                    continue
                if require_skill_md and "SKILL.md" not in files:
                    continue
                candidates.append(p)
        return (candidates[0] if len(candidates) == 1 else None), candidates

    @staticmethod
    def _nested_skill_dirs(abs_dir: str) -> List[str]:
        """列出 abs_dir 严格下层的其它技能目录（用于阻止"删一个技能连带删掉整包"）。"""
        nested: List[str] = []
        if not abs_dir:
            return nested
        target = os.path.normpath(abs_dir)
        for root, dirs, files in os.walk(target):
            _prune_hidden(dirs)
            p = os.path.normpath(root)
            if p != target and "SKILL.md" in files:
                nested.append(p)
        return nested

    @staticmethod
    def _agents_dir() -> str:
        return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "agents")

    @classmethod
    def _abs_from_rel(cls, rel_path: Optional[str]) -> Optional[str]:
        """rel_path（相对 app/agents/，POSIX 风格）→ 绝对路径；空值返回 None。"""
        if not rel_path:
            return None
        return os.path.normpath(os.path.join(cls._agents_dir(), *str(rel_path).split("/")))

    @classmethod
    def _rel_from_abs(cls, abs_path: Optional[str]) -> Optional[str]:
        """绝对路径 → rel_path；不在 agents/ 下时返回 None（防越界写入）。"""
        if not abs_path:
            return None
        agents = os.path.normpath(cls._agents_dir())
        p = os.path.normpath(abs_path)
        if not p.startswith(agents + os.sep):
            return None
        return os.path.relpath(p, agents).replace(os.sep, "/")

    @staticmethod
    def _skills_root() -> str:
        return os.path.join(SkillService._agents_dir(), "skills")

    @staticmethod
    def _skills_roots() -> list:
        """三个技能根目录：(目录, 根类型)。None 表示可选技能目录（用户可见、可上传）。"""
        agents_dir = SkillService._agents_dir()
        return [
            (os.path.join(agents_dir, "skills"), None),
            (os.path.join(agents_dir, "skills_dialog"), ROOT_DIALOG),
            (os.path.join(agents_dir, "skills_task"), ROOT_TASK),
        ]

    def sync_from_filesystem(self) -> dict:
        """扫描技能目录并与数据库三方对账（纯本地：不读 README，不做任何网络调用）。

        磁盘是技能"存在与否"的唯一事实来源，身份用 rel_path（相对 app/agents/）：
        - 磁盘有、库里无 → 新增
        - 两边都有 → 更新 description / 归属包 / 内置标志 / rel_path（display_name、required_role 以库为准）
        - 库里有、磁盘无 → 停用（is_active=False + dir_missing=True）并逐条 WARNING；目录回来后自动恢复
        - 管理员主动软删除（dir_missing=False）的行不会被同步重新激活，避免"删了又复活"

        可选根目录整体缺失、或扫不出任何技能而库里还有启用中的可选技能时，判定为挂载/播种异常：
        跳过全部停用动作并返回 aborted（否则一次空卷就会把所有技能与技能包打成软删除）。
        """
        t0 = time.monotonic()
        with skill_mutation_lock():
            return self._sync_locked(t0)

    def _sync_locked(self, t0: float) -> dict:
        from app.services.skill_hub.access import managed_map
        _personal_skill_ids = set(managed_map(self.db))
        stats = {
            # deleted 保留旧字段名（= 本次停用的技能数），另附明细与新维度
            "created": 0, "updated": 0, "deleted": 0,
            "reactivated": 0, "packages_created": 0, "packages_deactivated": 0,
            "deactivated_skills": [], "deactivated_packages": [], "conflicts": [],
            "pending_meta_package_ids": [], "aborted": None, "elapsed_ms": 0,
        }
        entries = scan_skill_dirs(self._skills_roots())
        by_name, shadowed = index_skills_by_name(entries)
        stats["conflicts"].extend(shadowed)

        # 挂载/播种异常兜底：可选根扫不出技能，但库里还有启用中的可选技能 → 不停用任何记录
        opt_root = self._skills_root()
        optional_entries = [e for e in entries if e.root_kind == ROOT_OPTIONAL]
        active_optional_db = self.db.query(Skill).filter(
            Skill.is_active == True,          # noqa: E712
            Skill.builtin_dialog == False,    # noqa: E712
            Skill.builtin_task == False,      # noqa: E712
        ).count()
        if active_optional_db > 0 and (not os.path.isdir(opt_root) or not optional_entries):
            stats["aborted"] = "optional_root_missing" if not os.path.isdir(opt_root) else "optional_root_empty"
            stats["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
            print(f"[SkillService] [ERROR] 技能同步已中止（{stats['aborted']}）：可选技能目录 {opt_root} "
                  f"{'不存在' if stats['aborted'] == 'optional_root_missing' else '扫不出任何 SKILL.md'}，"
                  f"但库里还有 {active_optional_db} 个启用中的可选技能；疑似卷未挂载或播种失败，本次不停用任何记录")
            return stats

        # 判包走结构规则（detect_packages，与 README 无关）；包摘要生成排队到后台，不在此处调模型
        pkg_rel_by_skill = detect_packages(entries)
        pkg_id_by_rel, pending_meta = self._upsert_packages(sorted(set(pkg_rel_by_skill.values())))
        stats["packages_created"] = len(pending_meta)
        stats["pending_meta_package_ids"] = pending_meta

        # 同名技能同时存在于两个内置根时（共用技能）两个标志都置位
        flags_by_name: Dict[str, Dict[str, bool]] = {}
        for e in entries:
            f = flags_by_name.setdefault(e.name, {"dialog": False, "task": False})
            if e.root_kind == ROOT_DIALOG:
                f["dialog"] = True
            elif e.root_kind == ROOT_TASK:
                f["task"] = True

        touched_ids = set()
        for name, e in by_name.items():
            meta = e.meta
            description = " ".join(str(meta.get("description") or "").split()) or name
            display_name = str(meta.get("title") or meta.get("name") or name)
            flags = flags_by_name.get(name, {"dialog": False, "task": False})
            new_pkg_id = pkg_id_by_rel.get(pkg_rel_by_skill.get(e.rel_path))
            row_by_rel = self.db.query(Skill).filter(Skill.rel_path == e.rel_path).first()
            row_by_name = self.db.query(Skill).filter(Skill.name == name).first()
            if row_by_rel is not None and row_by_name is not None and row_by_rel.id != row_by_name.id:
                stats["conflicts"].append(
                    f"'{name}' 的路径 {e.rel_path} 与目录名分别命中不同记录"
                    f"（id={row_by_rel.id}/{row_by_name.id}），按路径记录处理"
                )
            skill = row_by_rel or row_by_name
            if skill is not None:
                touched_ids.add(skill.id)
                # 归属被管理员锁定过的行：磁盘上又出现"另一个根里的同名目录"（镜像种子回灌、
                # 手工复制等）时，不把身份改到那份副本上，只记一条冲突告警。
                # 归属以库里锁定值为准，避免 rel_path 与 builtin 标志互相打架。
                if skill.scope_locked and skill.rel_path != e.rel_path and scope_of_root(e.root_kind) != scope_of_flags(skill):
                    stats["conflicts"].append(
                        f"'{name}' 的归属已由管理员锁定为 {SCOPE_LABELS[scope_of_flags(skill)]}，"
                        f"但磁盘上 {e.rel_path} 也存在同名目录，本次跳过该行"
                    )
                    continue
                changed = False
                if skill.rel_path != e.rel_path:
                    skill.rel_path = e.rel_path
                    changed = True
                if skill.name != name:
                    # 目录改名：名字跟随磁盘，但 name 有唯一约束，被占用时保持原样并告警
                    taken = self.db.query(Skill).filter(Skill.name == name, Skill.id != skill.id).first()
                    if taken is None:
                        skill.name = name
                        changed = True
                    else:
                        stats["conflicts"].append(f"目录改名为 '{name}'，但该名字已被技能 id={taken.id} 占用，保持原名 '{skill.name}'")
                if skill.dir_missing:
                    skill.dir_missing = False
                    if not skill.is_active:
                        skill.is_active = True
                        stats["reactivated"] += 1
                    changed = True
                if skill.package_id != new_pkg_id:
                    skill.package_id = new_pkg_id
                    changed = True
                if skill.description != description:
                    skill.description = description
                    changed = True
                # 归属锁定时不按目录位置改写内置标志：change_scope 移动目录后两者本就一致，
                # 这里防的是同名副本/卷回灌造成的误判（上面的 mismatch 守卫已拦掉大部分）
                if not skill.scope_locked:
                    if bool(skill.builtin_dialog) != flags["dialog"]:
                        skill.builtin_dialog = flags["dialog"]
                        changed = True
                    if bool(skill.builtin_task) != flags["task"]:
                        skill.builtin_task = flags["task"]
                        changed = True
                if changed:
                    stats["updated"] += 1
            else:
                new_row = Skill(
                    name=name,
                    display_name=display_name,
                    description=description,
                    required_role=str(meta.get("required_role", "viewer")),
                    builtin_dialog=flags["dialog"],
                    builtin_task=flags["task"],
                    package_id=new_pkg_id,
                    rel_path=e.rel_path,
                    dir_missing=False,
                )
                self.db.add(new_row)
                # 立刻 flush 拿到 id 并记入 touched：会话 autoflush 关闭时查询看不到 pending 行，
                # 显式登记可以避免"刚新增就被同一轮收尾当成目录缺失停用"
                self.db.flush()
                touched_ids.add(new_row.id)
                stats["created"] += 1

        # 库里未被本轮命中的行 → 目录已不在：标记 dir_missing 并停用（保留记录，目录回来后自动恢复）
        for skill in self.db.query(Skill).all():
            if skill.id in _personal_skill_ids or skill.id in touched_ids:
                continue
            was_active = bool(skill.is_active)
            skill.dir_missing = True
            if settings.GOVERNANCE_RESOURCE_VERSIONING_ENABLED:
                from app.models.skill_governance import SkillPublication
                if self.db.get(SkillPublication, skill.id) is not None:
                    stats["conflicts"].append(f"{skill.name} 原目录缺失，继续使用已发布快照")
                    continue
            if was_active:
                skill.is_active = False
                stats["deleted"] += 1
                stats["deactivated_skills"].append(f"{skill.name}（{skill.rel_path or '路径未记录'}）")

        touched_pkg_ids = set(pkg_id_by_rel.values())
        _personal_packages = {s.package_id for s in self.db.query(Skill).filter(Skill.id.in_(_personal_skill_ids)) if s.package_id}
        for pkg in self.db.query(SkillPackage).all():
            if pkg.id in _personal_packages or pkg.id in touched_pkg_ids:
                continue
            was_active = bool(pkg.is_active)
            pkg.dir_missing = True
            if was_active:
                pkg.is_active = False
                stats["packages_deactivated"] += 1
                stats["deactivated_packages"].append(f"{pkg.name}（{pkg.rel_path or '路径未记录'}）")

        self.db.commit()
        if settings.GOVERNANCE_RESOURCE_VERSIONING_ENABLED and not stats["aborted"]:
            from app.services.skill_versions import create_snapshot, source_changed
            stats["pending_versions"] = []
            for skill in self.db.query(Skill).filter(Skill.id.in_(touched_ids)).all():
                try:
                    if source_changed(self.db, skill):
                        candidate = create_snapshot(self.db, skill.id)
                        stats["pending_versions"].append(candidate.id)
                except Exception as error:
                    stats["conflicts"].append(f"{skill.name} 候选快照创建失败: {type(error).__name__}")
        stats["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
        self._log_sync_result(stats)
        if pending_meta:
            # 只排队包摘要；技能中文名由调用方（上传作业/刷新/启动）自行排队
            spawn_skill_enrichment(package_ids=pending_meta, translate=False)
        return stats

    @staticmethod
    def _log_sync_result(stats: dict) -> None:
        """同步结果结构化落日志：软删除必须留名，排障时不用再去猜是哪个包被停用了。"""
        print(f"[SkillService] 同步完成: 新增 {stats['created']}, 更新 {stats['updated']}, "
              f"停用 {stats['deleted']}, 恢复 {stats['reactivated']}, 待生成摘要的包 {stats['packages_created']}, "
              f"停用包 {stats['packages_deactivated']}, 耗时 {stats['elapsed_ms']}ms"
              + (f", 已中止={stats['aborted']}" if stats.get("aborted") else ""))
        for line in stats.get("deactivated_skills", []):
            print(f"[SkillService] [WARN] 停用技能（磁盘上找不到目录）: {line}")
        for line in stats.get("deactivated_packages", []):
            print(f"[SkillService] [WARN] 停用技能包（磁盘上找不到目录）: {line}")
        for line in stats.get("conflicts", []):
            print(f"[SkillService] [WARN] 技能目录冲突: {line}")

    # ==================== 中文展示信息生成 ====================

    # 翻译用模型：轻量快速，只做展示文案生成（用户指定）
    TRANSLATE_MODEL = "ali-qwen3.8-flash"

    @staticmethod
    def _has_cjk(text: str) -> bool:
        return any("\u4e00" <= ch <= "\u9fff" for ch in (text or ""))

    def translate_pending_skills(self, batch_size: int = 8) -> dict:
        """为展示名/描述是英文的技能生成中文展示信息。

        不是逐字直译：要求模型生成"精简概括功能的中文名"和"精简完整的中文描述"。
        只处理 display_name_zh/description_zh 为空且原文含英文的技能；
        结果落库，后续启动不会重复翻译。
        """
        import json as _json
        import httpx

        if not settings.AI_GATEWAY_API_KEY:
            return {"translated": 0, "pending_total": 0, "note": "no api key"}

        from app.services.skill_hub.access import managed_map
        personal_ids = set(managed_map(self.db))
        pending = [
            s for s in self.db.query(Skill).filter(Skill.is_active == True).all() if s.id not in personal_ids
            if s.display_name_zh is None and s.description_zh is None
            and not (self._has_cjk(s.display_name) and self._has_cjk(s.description or ""))
        ]
        translated = 0
        prompt_head = (
            "你是技能商店的中文本地化编辑。下面是若干 AI 技能的原始信息"
            "（JSON 数组，字段：name、display_name、description，内容多为英文）。\n"
            "请为每个技能生成中文展示信息，要求：\n"
            "1. display_name_zh：4~10 个汉字的名称，精炼概括技能的核心功能；"
            "可保留必要的英文产品名或缩写（如 ECS、PDF、FDC）；不要包含“技能”二字。\n"
            "2. description_zh：30~60 个汉字，精简而完整地说明这个技能能做什么、适合什么场景；"
            "在理解原文后重新组织语言，不要逐字直译。\n"
            "严格只输出一个 JSON 数组，元素为 "
            '{"name": 原name值, "display_name_zh": "...", "description_zh": "..."}，'
            "不要输出任何其他文字或代码块标记。\n\n"
        )
        for i in range(0, len(pending), batch_size):
            batch = pending[i:i + batch_size]
            items = [
                {"name": s.name, "display_name": s.display_name, "description": s.description or ""}
                for s in batch
            ]
            try:
                resp = httpx.post(
                    endpoint("chat/completions"),
                    headers=gateway_headers(),
                    json={
                        "model": self.TRANSLATE_MODEL,
                        "temperature": 0.3,
                        "messages": [
                            {"role": "user", "content": prompt_head + _json.dumps(items, ensure_ascii=False)}
                        ],
                    },
                    timeout=120,
                )
                resp.raise_for_status()
                content = (resp.json()["choices"][0]["message"]["content"] or "").strip()
                # 容错：模型偶尔会包一层 ```json ... ```
                if content.startswith("```"):
                    content = content.strip("`").strip()
                    if content.lower().startswith("json"):
                        content = content[4:].strip()
                arr = _json.loads(content)
                by_name = {it.get("name"): it for it in arr if isinstance(it, dict)}
                for s in batch:
                    it = by_name.get(s.name) or {}
                    name_zh = str(it.get("display_name_zh") or "").strip()[:200]
                    desc_zh = str(it.get("description_zh") or "").strip()[:500]
                    if name_zh or desc_zh:
                        s.display_name_zh = name_zh or None
                        s.description_zh = desc_zh or None
                        translated += 1
                self.db.commit()
            except Exception as e:
                print(f"[SkillService] 中文翻译批次失败（跳过）: {e}")
        return {"translated": translated, "pending_total": len(pending)}

    # ==================== 技能包 ====================

    def _inactive_package_ids(self) -> list:
        rows = self.db.query(SkillPackage.id).filter(SkillPackage.is_active == False).all()
        return [r[0] for r in rows]

    def _upsert_packages(self, pkg_rels: List[str]) -> Tuple[Dict[str, int], List[int]]:
        """按 rel_path upsert 包记录，返回 ({包 rel_path: id}, 待生成摘要的包 id 列表)。

        本方法不做任何网络调用：新包先用目录名占位，摘要生成由调用方排队到后台富化线程，
        避免"同步流程里串行调模型"把事件循环冻住（历史故障根因之一）。
        也不自行提交：一次同步只有一个事务，由 sync 收尾统一 commit。
        """
        mapping: Dict[str, int] = {}
        pending: List[int] = []
        for rel in pkg_rels:
            name = rel.rstrip("/").split("/")[-1]
            pkg = self.db.query(SkillPackage).filter(SkillPackage.rel_path == rel).first()
            if pkg is None:
                pkg = self.db.query(SkillPackage).filter(SkillPackage.name == name).first()
            if pkg is None:
                pkg = SkillPackage(name=name, display_name=name, description=None,
                                   rel_path=rel, dir_missing=False)
                self.db.add(pkg)
                self.db.flush()
                pending.append(pkg.id)
            else:
                if pkg.rel_path != rel:
                    pkg.rel_path = rel
                if pkg.dir_missing:
                    pkg.dir_missing = False
                    if not pkg.is_active:
                        # 目录回来了：撤销"因目录缺失而停用"；管理员主动删除的包不在此列
                        pkg.is_active = True
                if pkg.display_name == pkg.name and not pkg.description:
                    pending.append(pkg.id)
            mapping[rel] = pkg.id
        return mapping, pending

    # ---------- 包展示信息生成：素材只取自 SKILL.md（平台不读 README） ----------

    @staticmethod
    def _read_skill_md(dir_path: str) -> Optional[dict]:
        """读取目录下 SKILL.md 的 frontmatter（不存在或解析失败返回 None）。"""
        path = os.path.join(dir_path, "SKILL.md")
        if not os.path.isfile(path):
            return None
        try:
            meta = frontmatter.load(path).metadata
            return meta if isinstance(meta, dict) else None
        except Exception as e:
            print(f"[SkillService] SKILL.md 读取失败（{path}）: {e}")
            return None

    @staticmethod
    def _member_skill_lines(pkg_dir: str, limit: int = 12) -> List[str]:
        """汇总包内 SKILL.md 的 name/description 作为生成素材（含包目录自身那个技能）。"""
        lines: List[str] = []
        for root, dirs, files in os.walk(os.path.normpath(pkg_dir)):
            _prune_hidden(dirs)
            if "SKILL.md" not in files:
                continue
            meta = SkillService._read_skill_md(root) or {}
            name = str(meta.get("name") or os.path.basename(root))
            desc = " ".join(str(meta.get("description") or "").split())[:120]
            lines.append(f"- {name}: {desc}" if desc else f"- {name}")
            if len(lines) >= limit:
                break
        return lines

    def generate_package_meta(self, package_id: int, force: bool = False) -> bool:
        """为技能包生成"简短包名 + ~50 字描述"，素材只来自 SKILL.md。

        由后台富化线程调用（见 spawn_skill_enrichment），不在请求路径与同步流程里执行：
        单次模型调用超时 120s，放在事件循环里会把整个服务冻住。
        幂等：已生成过的包（短名不等于目录名，或已有描述）默认跳过，force=True 才重算。
        """
        pkg = self.db.query(SkillPackage).filter(SkillPackage.id == package_id).first()
        if not pkg:
            return False
        if not force and not (pkg.display_name == pkg.name and not pkg.description):
            return False
        pkg_dir, _candidates = self._resolve_package_dir(pkg.rel_path, pkg.name)
        if not pkg_dir:
            print(f"[SkillService] 包 '{pkg.name}' 目录不在（{pkg.rel_path or '路径未记录'}），跳过摘要生成")
            return False

        display_name = description = None
        own_meta = self._read_skill_md(pkg_dir)
        if own_meta:
            # 包自身 SKILL.md 已是中文 → 直接采用，省一次模型调用
            cand_name = str(own_meta.get("title") or own_meta.get("name") or "").strip()
            cand_desc = " ".join(str(own_meta.get("description") or "").split()).strip()
            if self._has_cjk(cand_name) and self._has_cjk(cand_desc):
                display_name, description = cand_name[:200], cand_desc[:500]
        if not display_name:
            generated = self._llm_package_meta(pkg.name, own_meta, pkg_dir)
            if generated:
                display_name = generated.get("display_name")
                description = generated.get("description")
        if not display_name and not description:
            # 无 key / 调用失败的兜底：用 SKILL.md 里的成员技能名拼一句，保证前端不显示空包名。
            # 描述非空即视为"已生成"，不会被下一轮同步反复重试。
            members = self._member_skill_lines(pkg_dir, limit=6)
            display_name = pkg.name
            description = (f"包含 {len(members)} 个技能：" +
                           "、".join(m.lstrip("- ").split(":")[0] for m in members))[:500]
        pkg.display_name = (display_name or pkg.name)[:200]
        if description:
            pkg.description = description[:500]
        self.db.commit()
        print(f"[SkillService] 已生成技能包展示信息: {pkg.name} → {pkg.display_name}")
        return True

    def _llm_package_meta(self, pkg_name: str, own_meta: Optional[dict], pkg_dir: str) -> Optional[dict]:
        """调轻量模型生成包展示信息；失败返回 None（由调用方兜底）。"""
        import json as _json
        import httpx

        if not settings.AI_GATEWAY_API_KEY:
            return None
        parts = []
        if own_meta:
            own_name = str(own_meta.get("title") or own_meta.get("name") or pkg_name)
            own_desc = " ".join(str(own_meta.get("description") or "").split())[:300]
            parts.append(f"包自身的 SKILL.md：{own_name}" + (f" —— {own_desc}" if own_desc else ""))
        members = self._member_skill_lines(pkg_dir)
        if members:
            parts.append("包内成员技能（名称: 描述）：\n" + "\n".join(members))
        if not parts:
            return None
        prompt = (
            f"技能包目录名：{pkg_name}\n\n" + "\n\n".join(parts) + "\n\n"
            "请为这个技能包生成展示信息，要求：\n"
            "1. display_name：4~6 个汉字的简短包名，精炼概括包的定位（如'阿里工具包'、'WIP工具包'），可保留必要英文缩写；\n"
            "2. description：约 50 个汉字，概括这个技能包包含的能力和适用场景。\n"
            '严格只输出 JSON：{"display_name": "...", "description": "..."}，不要输出任何其他文字或代码块标记。'
        )
        try:
            resp = httpx.post(
                endpoint("chat/completions"),
                headers=gateway_headers(),
                json={
                    "model": self.TRANSLATE_MODEL,
                    "temperature": 0.3,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=120,
            )
            resp.raise_for_status()
            text = (resp.json()["choices"][0]["message"]["content"] or "").strip()
            if text.startswith("```"):
                text = text.strip("`").strip()
                if text.lower().startswith("json"):
                    text = text[4:].strip()
            obj = _json.loads(text)
            if isinstance(obj, dict):
                return {
                    "display_name": str(obj.get("display_name") or "").strip()[:200] or None,
                    "description": str(obj.get("description") or "").strip()[:500] or None,
                }
        except Exception as e:
            print(f"[SkillService] 包摘要生成失败（{pkg_name}）: {e}")
        return None

    def get_all_packages_include_deleted(self) -> list:
        from app.services.skill_hub.access import system_only
        return system_only(self.db.query(SkillPackage), self.db, SkillPackage).order_by(SkillPackage.created_at, SkillPackage.id).all()

    def get_package_by_id(self, package_id: int) -> SkillPackage:
        pkg = self.db.query(SkillPackage).filter(SkillPackage.id == package_id).first()
        if not pkg:
            raise ValueError("技能包不存在")
        return pkg

    def update_package(self, package_id: int, display_name: str = None, description: str = None,
                       required_role: str = None, is_hidden: bool = None) -> SkillPackage:
        pkg = self.get_package_by_id(package_id)
        if display_name is not None:
            pkg.display_name = display_name
        if description is not None:
            pkg.description = description
        if required_role is not None:
            if required_role == "":
                # 清除包级权限：成员技能恢复各自独立权限（不做任何改动）
                pkg.required_role = None
            else:
                # 设置包级权限：包内所有成员技能统一沿用该权限
                pkg.required_role = required_role
                self.db.query(Skill).filter(Skill.package_id == pkg.id).update(
                    {Skill.required_role: required_role}, synchronize_session=False
                )
        if is_hidden is not None:
            pkg.is_hidden = bool(is_hidden)
        self.db.commit()
        self.db.refresh(pkg)
        return pkg

    def delete_package(self, package_id: int):
        """软删除包（管理员意图）：成员技能保留、用户端整体隐藏；同步不会把它重新激活。"""
        pkg = self.get_package_by_id(package_id)
        with skill_mutation_lock():
            pkg.is_active = 0
            pkg.dir_missing = False
            self.db.commit()

    def restore_package(self, package_id: int):
        """恢复包：先确认包目录还在，否则抛 SkillDirMissingError（API 转 409）。

        目录不在时翻牌没有意义——下一次同步按"磁盘是唯一事实来源"会再打回，
        管理面板就会出现"恢复了又自动被软删除"的死循环。
        """
        pkg = self.get_package_by_id(package_id)
        with skill_mutation_lock():
            resolved, _candidates = self._resolve_package_dir(pkg.rel_path, pkg.name)
            if not resolved:
                raise SkillDirMissingError(pkg.name, pkg.rel_path)
            pkg.rel_path = self._rel_from_abs(resolved) or pkg.rel_path
            pkg.is_active = 1
            pkg.dir_missing = False
            self.db.commit()

    def hard_delete_package(self, package_id: int) -> dict:
        """硬删除包：包记录 + 全部成员技能记录 + 包目录（含成员目录）。

        目录一律按库里记录的 rel_path 定位，不再按目录名在多个根里递归猜；
        目录已不在时只清 DB 记录——僵尸包也能被清掉，不会"删不掉又反复被软删除"。
        """
        pkg = self.get_package_by_id(package_id)
        with skill_mutation_lock():
            members = self.db.query(Skill).filter(Skill.package_id == pkg.id).all()
            pkg_dir, candidates = self._resolve_package_dir(pkg.rel_path, pkg.name)
            if pkg_dir is None and len(candidates) > 1:
                raise SkillDeleteConflictError(
                    f"技能包 '{pkg.name}' 在磁盘上对应多个目录（{', '.join(candidates)}），无法确定删哪一个"
                )
            # 成员目录若不在包目录下（历史脏数据），单独清理，避免残留目录被下次同步重新注册
            outside_dirs = []
            pkg_norm = os.path.normpath(pkg_dir) if pkg_dir else None
            for s in members:
                d = self._abs_from_rel(s.rel_path)
                if not d or not os.path.isdir(d):
                    continue
                # 包根自身也可能是一条技能（目录里有 SKILL.md）：它不算"包外残留"
                inside = pkg_norm and (d == pkg_norm or d.startswith(pkg_norm + os.sep))
                if not inside:
                    outside_dirs.append(d)
            member_names = [s.name for s in members]
            from app.services.resource_deletion import remove_skill_history
            snapshot_dirs = remove_skill_history(self.db, [member.id for member in members], member_names)
            from app.services.resource_deletion import remove_resource_history
            snapshot_dirs.extend(remove_resource_history(self.db, 'package', pkg.id))
            from app.models.user_skill_access import UserSkillAccess
            member_ids = [s.id for s in members]
            if member_ids:
                self.db.query(UserSkillAccess).filter(
                    UserSkillAccess.target_type == "skill",
                    UserSkillAccess.target_id.in_(member_ids),
                ).delete(synchronize_session=False)
            for s in members:
                self.db.delete(s)
            name, rel_path = pkg.name, pkg.rel_path
            if rel_path and rel_path.startswith('skills/') and '/' not in rel_path[len('skills/'):]:
                self._update_seed_tracker(ROOT_OPTIONAL, rel_path[len('skills/'):], suppress=True)
            self.db.query(UserSkillAccess).filter(
                UserSkillAccess.target_type == "package",
                UserSkillAccess.target_id == pkg.id,
            ).delete(synchronize_session=False)
            self.db.delete(pkg)
            self.db.commit()
            for snapshot_dir in snapshot_dirs:
                shutil.rmtree(snapshot_dir)
            removed = False
            if pkg_dir:
                shutil.rmtree(pkg_dir, ignore_errors=True)
                removed = True
            for d in outside_dirs:
                shutil.rmtree(d, ignore_errors=True)
            if removed:
                print(f"[SkillService] 彻底删除技能包 '{name}'（{rel_path or pkg_dir}），"
                      f"连带 {len(member_names)} 个成员技能: {', '.join(member_names[:10])}"
                      + (f"（另清理包外残留目录 {len(outside_dirs)} 个）" if outside_dirs else ""))
            else:
                print(f"[SkillService] 彻底删除技能包 '{name}'：磁盘目录不存在，仅清理 "
                      f"{len(member_names)} 条数据库记录")
            return {"name": name, "members": member_names, "dir_removed": removed,
                    "db_only": not removed, "outside_dirs_removed": len(outside_dirs)}

    def _resolve_package_dir(self, rel_path: Optional[str], name: str) -> Tuple[Optional[str], List[str]]:
        """定位包目录：优先库里记录的 rel_path；缺失时只在可选根下按目录名找，多候选不猜。

        包目录本身可以没有 SKILL.md（成员在下层），所以这里不要求 SKILL.md。
        """
        by_rel = self._abs_from_rel(rel_path)
        if by_rel and os.path.isdir(by_rel):
            return by_rel, [by_rel]
        opt_root = os.path.normpath(self._skills_root())
        candidates: List[str] = []
        if os.path.isdir(opt_root):
            for root, dirs, _files in os.walk(opt_root):
                _prune_hidden(dirs)
                p = os.path.normpath(root)
                if p != opt_root and os.path.basename(p) == name:
                    candidates.append(p)
        return (candidates[0] if len(candidates) == 1 else None), candidates

    def _find_package_directory(self, name: str) -> Optional[str]:
        """兼容旧调用：按目录名找包目录；命中多个时返回 None（不猜）。"""
        resolved, _candidates = self._resolve_package_dir(None, name)
        return resolved
