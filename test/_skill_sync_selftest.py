"""技能同步/上传作业自检（不依赖服务器与真实库）。

用临时目录树 + 临时 SQLite 跑真实代码路径，覆盖历史上出过问题的场景：
  1. 结构判包（无 README 也成包；单技能顶层目录是独立技能）
  2. .staging-* 暂存目录不被注册
  3. 同名技能跨内置根合并标志、rel_path 跨根唯一
  4. 目录消失 → 停用并标记 dir_missing；目录回来 → 自动恢复
  5. 管理员主动软删除不会被同步重新激活
  6. 目录缺失时 restore 抛 SkillDirMissingError（不再"恢复了又被打回"）
  7. 包根技能（目录里还嵌着别的技能）禁止单技能彻底删除
  8. 可选根被清空 → 同步中止，绝不误删全部技能
  9. 变更锁被占用 → SkillBusyError（API 层转 409）
 10. 无模型 key 时包摘要有兜底，且不会被反复重试

运行（任意目录，脚本会自己切到 backend；容器内可用 SKILL_BACKEND_DIR=/app 指定）：
    python test/_skill_sync_selftest.py
"""
import os
import shutil
import sys
import tempfile
import threading
import traceback


def _resolve_backend_dir():
    env_dir = os.environ.get("SKILL_BACKEND_DIR")
    if env_dir:
        return os.path.abspath(env_dir)
    here = os.path.dirname(os.path.abspath(__file__))
    candidate = os.path.normpath(os.path.join(here, "..", "backend"))
    if os.path.isdir(os.path.join(candidate, "app")):
        return candidate
    if os.path.isdir(os.path.join(os.getcwd(), "app")):
        return os.getcwd()
    return candidate


BACKEND_DIR = _resolve_backend_dir()
os.chdir(BACKEND_DIR)
sys.path.insert(0, BACKEND_DIR)

from sqlalchemy import create_engine                      # noqa: E402
from sqlalchemy.orm import sessionmaker                   # noqa: E402

RESULTS = []


def check(name, cond, extra=""):
    RESULTS.append((name, bool(cond)))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  {extra}" if extra and not cond else ""))


def write_skill(dir_path, name, description="E2E skill", role="viewer", cjk=False):
    os.makedirs(dir_path, exist_ok=True)
    desc = f"{name} 的中文描述" if cjk else description
    title = f"{name} 中文名" if cjk else name
    with open(os.path.join(dir_path, "SKILL.md"), "w", encoding="utf-8") as f:
        f.write(f"---\nname: {name}\ntitle: {title}\ndescription: {desc}\nrequired_role: {role}\n---\n\n# {name}\n")


def main():
    tmp = tempfile.mkdtemp(prefix="skill-selftest-")
    agents_dir = os.path.join(tmp, "agents")
    opt = os.path.join(agents_dir, "skills")
    dialog = os.path.join(agents_dir, "skills_dialog")
    task = os.path.join(agents_dir, "skills_task")

    # ---------- 构造磁盘：2 个包 + 1 个独立技能 + 内置同名技能 + 暂存目录 ----------
    write_skill(os.path.join(opt, "pkgA", "s1"), "s1")
    write_skill(os.path.join(opt, "pkgA", "nested", "s2"), "s2")
    write_skill(os.path.join(opt, "solo"), "solo")
    write_skill(os.path.join(opt, "pkgB"), "pkgB")                       # 包根自身也是技能
    write_skill(os.path.join(opt, "pkgB", "inner"), "inner", cjk=True)
    write_skill(os.path.join(dialog, "d1"), "d1")
    write_skill(os.path.join(task, "d1"), "d1")                          # 同名跨内置根
    write_skill(os.path.join(opt, ".staging-99", "ghost"), "ghost")      # 半成品，必须被忽略
    with open(os.path.join(opt, "pkgA", "README.md"), "w", encoding="utf-8") as f:
        f.write("# README 不应参与任何判定\n")

    # ---------- 把 SkillService 指向临时目录 + 临时库 ----------
    from app.services import skill_service as ss
    from app.services.skill_service import SkillService
    from app.database import Base
    from app.models.skill import Skill
    from app.models.skill_package import SkillPackage

    SkillService._agents_dir = staticmethod(lambda: agents_dir)
    enrich_calls = []
    ss.spawn_skill_enrichment = lambda package_ids=None, translate=True: enrich_calls.append(
        (list(package_ids or []), translate)) or 0

    engine = create_engine(f"sqlite:///{os.path.join(tmp, 'test.db')}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine, tables=[Skill.__table__, SkillPackage.__table__])
    db = sessionmaker(bind=engine, autocommit=False, autoflush=False)()
    svc = SkillService(db)

    try:
        # ---------- 1~3 首次同步 ----------
        stats = svc.sync_from_filesystem()
        check("首次同步无中止", stats["aborted"] is None, str(stats))
        pkgs = {p.name: p for p in db.query(SkillPackage).all()}
        skills = {s.name: s for s in db.query(Skill).all()}
        check("无 README 也按结构建包（pkgA/pkgB）", set(pkgs) == {"pkgA", "pkgB"}, str(list(pkgs)))
        check("单技能顶层目录是独立技能（solo 无包）",
              "solo" in skills and skills["solo"].package_id is None)
        check("深层嵌套技能归属一级包（s2 → pkgA）",
              skills["s2"].package_id == pkgs["pkgA"].id, str(skills["s2"].rel_path))
        check("包根自身的 SKILL.md 也是包成员（pkgB）",
              skills["pkgB"].package_id == pkgs["pkgB"].id)
        check("rel_path 落库且带根段", skills["s1"].rel_path == "skills/pkgA/s1"
              and skills["d1"].rel_path == "skills_dialog/d1", str(skills["d1"].rel_path))
        check("同名内置技能合并两个标志",
              bool(skills["d1"].builtin_dialog) and bool(skills["d1"].builtin_task))
        check("暂存目录 .staging-* 未被注册", "ghost" not in skills)
        check("新包已排队后台生成摘要（不在同步里调模型）",
              any(ids for ids, _t in enrich_calls), str(enrich_calls))

        # ---------- 10 无 key 兜底：包摘要不空转 ----------
        from app.config import settings
        old_key = settings.DASHSCOPE_API_KEY
        settings.DASHSCOPE_API_KEY = ""
        try:
            pkgB = pkgs["pkgB"]
            ok = svc.generate_package_meta(pkgB.id, force=True)
            db.refresh(pkgB)
            check("包摘要生成成功（包自身 SKILL.md 为中文时直接采用）", ok and bool(pkgB.description),
                  f"{pkgB.display_name}/{pkgB.description}")
            pkgA = pkgs["pkgA"]
            svc.generate_package_meta(pkgA.id, force=True)
            db.refresh(pkgA)
            check("无模型 key 时包摘要有兜底描述（不会被反复重试）",
                  bool(pkgA.description), f"{pkgA.display_name}/{pkgA.description}")
        finally:
            settings.DASHSCOPE_API_KEY = old_key

        # ---------- 4 目录消失 → 停用 + dir_missing ----------
        shutil.rmtree(os.path.join(opt, "pkgA"))
        stats = svc.sync_from_filesystem()
        db.expire_all()
        pkgs = {p.name: p for p in db.query(SkillPackage).all()}
        skills = {s.name: s for s in db.query(Skill).all()}
        check("目录消失：包被停用并标记 dir_missing",
              not pkgs["pkgA"].is_active and pkgs["pkgA"].dir_missing)
        check("目录消失：成员技能被停用并计数", stats["deleted"] == 2, str(stats["deleted"]))
        check("停用明细留名（排障可见）", len(stats["deactivated_skills"]) == 2
              and stats["packages_deactivated"] == 1, str(stats))

        # ---------- 6 目录缺失时恢复必须被拒 ----------
        try:
            svc.restore_package(pkgs["pkgA"].id)
            check("目录缺失时 restore_package 抛 SkillDirMissingError", False)
        except ss.SkillDirMissingError:
            check("目录缺失时 restore_package 抛 SkillDirMissingError", True)
        try:
            svc.restore_skill(skills["s1"].id)
            check("目录缺失时 restore_skill 抛 SkillDirMissingError", False)
        except ss.SkillDirMissingError:
            check("目录缺失时 restore_skill 抛 SkillDirMissingError", True)

        # ---------- 4b 目录回来 → 自动恢复 ----------
        write_skill(os.path.join(opt, "pkgA", "s1"), "s1")
        write_skill(os.path.join(opt, "pkgA", "nested", "s2"), "s2")
        stats = svc.sync_from_filesystem()
        db.expire_all()
        pkgs = {p.name: p for p in db.query(SkillPackage).all()}
        skills = {s.name: s for s in db.query(Skill).all()}
        check("目录回来：包与成员自动恢复", pkgs["pkgA"].is_active and not pkgs["pkgA"].dir_missing
              and skills["s1"].is_active and stats["reactivated"] == 2, str(stats))

        # ---------- 5 管理员软删除不被同步冲掉 ----------
        svc.delete_skill(skills["s1"].id)
        svc.sync_from_filesystem()
        db.expire_all()
        skills = {s.name: s for s in db.query(Skill).all()}
        check("管理员软删除后同步不再自动激活", not skills["s1"].is_active
              and not skills["s1"].dir_missing)
        svc.restore_skill(skills["s1"].id)
        db.expire_all()
        check("目录在时 restore 成功", bool(skills["s1"].is_active))

        # ---------- 7 包根技能禁止单技能彻底删除 ----------
        skills = {s.name: s for s in db.query(Skill).all()}
        try:
            svc.hard_delete_skill(skills["pkgB"].id)
            check("包根技能（内嵌其它技能）禁止单技能彻底删除", False)
        except ss.SkillDeleteConflictError:
            check("包根技能（内嵌其它技能）禁止单技能彻底删除", True)
        check("拒绝后包目录仍在磁盘", os.path.isdir(os.path.join(opt, "pkgB")))

        res = svc.hard_delete_skill(skills["s2"].id)
        check("普通成员技能可彻底删除（含目录）", res["dir_removed"]
              and not os.path.isdir(os.path.join(opt, "pkgA", "nested", "s2")))

        res = svc.hard_delete_package(pkgs["pkgB"].id)
        db.expire_all()
        check("包级彻底删除：目录与成员记录一起清掉",
              res["dir_removed"] and not os.path.isdir(os.path.join(opt, "pkgB"))
              and db.query(Skill).filter(Skill.name == "inner").first() is None)

        # ---------- 9 变更锁 ----------
        holder = threading.Thread(target=lambda: _hold_lock(1.0), daemon=True)
        holder.start()
        import time as _t
        _t.sleep(0.2)
        try:
            svc.sync_from_filesystem()
            check("锁被占用时同步抛 SkillBusyError", False)
        except ss.SkillBusyError:
            check("锁被占用时同步抛 SkillBusyError", True)
        holder.join(timeout=3)

        # ---------- 8 可选根被清空 → 同步中止，绝不误删 ----------
        shutil.rmtree(opt)
        os.makedirs(opt)
        stats = svc.sync_from_filesystem()
        db.expire_all()
        active = db.query(Skill).filter(Skill.is_active == True).count()  # noqa: E712
        check("空可选根：同步中止且未停用任何记录",
              stats["aborted"] == "optional_root_empty" and stats["deleted"] == 0 and active > 0,
              str(stats))

        # ---------- 僵尸记录：目录不在也能只清 DB ----------
        shutil.rmtree(os.path.join(opt), ignore_errors=True)
        os.makedirs(os.path.join(dialog, "d1"), exist_ok=True)
        write_skill(os.path.join(dialog, "d1"), "d1")
        stats = svc.sync_from_filesystem()   # 可选根缺失但库里可选技能 active → 仍应中止
        check("可选根缺失时持续中止（内置根存在也不误判）",
              stats["aborted"] in ("optional_root_missing", "optional_root_empty")
              and stats["deleted"] == 0, str(stats["aborted"]))
    except Exception:
        traceback.print_exc()
        check("自检运行未抛异常", False)
    finally:
        db.close()
        shutil.rmtree(tmp, ignore_errors=True)

    failed = [n for n, ok in RESULTS if not ok]
    print(f"\n===== 技能同步自检：{len(RESULTS) - len(failed)}/{len(RESULTS)} 通过 =====")
    if failed:
        print("失败项: " + "; ".join(failed))
    return 1 if failed else 0


def _hold_lock(seconds):
    from app.services.skill_service import skill_mutation_lock
    import time
    with skill_mutation_lock():
        time.sleep(seconds)


if __name__ == "__main__":
    sys.exit(main())
