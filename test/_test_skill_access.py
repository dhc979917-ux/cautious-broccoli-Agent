"""技能广场可见性 + 用户技能授权 回归测试（本地后端须运行在 :8000）。

覆盖决策 B：
- 隐藏技能：管理员可见；未授权非管理员在 GET /skills/ 不可见且不可用（角色达标也不可用）
- 用户授权：覆盖角色门槛与隐藏（广场可见 + 聊天可用）
- 技能包隐藏：整包成员不可见；包授权用户仍可见可用
- 授权移除后即时失效

用法（仓库根目录）：
    python test/_test_skill_access.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.environ.get("SKILL_TEST_BACKEND") or os.path.normpath(os.path.join(HERE, "..", "backend"))
os.chdir(BACKEND)
sys.path.insert(0, BACKEND)

import httpx  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.core.security import get_password_hash  # noqa: E402
from app.models.user import User  # noqa: E402
from app.models.skill import Skill  # noqa: E402
from app.models.skill_package import SkillPackage  # noqa: E402
from app.models.user_skill_access import UserSkillAccess  # noqa: E402
from app.services.skill_service import SkillService  # noqa: E402
from app.core.cache import cache_delete_pattern  # noqa: E402

BASE = os.environ.get("SKILL_TEST_BASE") or "http://127.0.0.1:8000/api/v1"
P = "_SKILLVIS_"
PW = "SkillVis#2026"

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  | {detail}" if detail and not cond else ""))


def purge(db):
    cache_delete_pattern("skills:*")
    ids = [u.id for u in db.query(User).filter(User.account.like(f"{P}%")).all()]
    if ids:
        db.query(UserSkillAccess).filter(UserSkillAccess.user_id.in_(ids)).delete(synchronize_session=False)
        db.query(User).filter(User.id.in_(ids)).delete(synchronize_session=False)
    sids = [s.id for s in db.query(Skill).filter(Skill.name.like(f"{P}%")).all()]
    if sids:
        db.query(UserSkillAccess).filter(
            UserSkillAccess.target_type == "skill",
            UserSkillAccess.target_id.in_(sids),
        ).delete(synchronize_session=False)
        db.query(Skill).filter(Skill.id.in_(sids)).delete(synchronize_session=False)
    pids = [p.id for p in db.query(SkillPackage).filter(SkillPackage.name.like(f"{P}%")).all()]
    if pids:
        db.query(UserSkillAccess).filter(
            UserSkillAccess.target_type == "package",
            UserSkillAccess.target_id.in_(pids),
        ).delete(synchronize_session=False)
        db.query(SkillPackage).filter(SkillPackage.id.in_(pids)).delete(synchronize_session=False)
    db.commit()


def login(account):
    r = httpx.post(f"{BASE}/auth/login", json={"account": account, "password": PW}, timeout=10)
    r.raise_for_status()
    return r.json()["access_token"]


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def skill_names(token):
    r = httpx.get(f"{BASE}/skills/", headers=auth(token), timeout=15)
    r.raise_for_status()
    return {s["name"]: s for s in r.json()}


def usable_names(db, account):
    # 结束本进程的旧读快照（setup 的 refresh 会开启长事务），确保读到后端最新提交
    db.rollback()
    user = db.query(User).filter(User.account == account).first()
    return {s.name for s in SkillService(db).get_usable_skills_for_user(user)}


def main():
    db = SessionLocal()
    try:
        purge(db)
        admin = User(account=f"{P}admin", username=f"{P}管理员", role="admin",
                     password_hash=get_password_hash(PW), is_active=True)
        op = User(account=f"{P}op", username=f"{P}操作员", role="operator",
                  password_hash=get_password_hash(PW), is_active=True)
        viewer = User(account=f"{P}viewer", username=f"{P}观察者", role="viewer",
                      password_hash=get_password_hash(PW), is_active=True)
        db.add_all([admin, op, viewer])
        db.commit()
        db.refresh(admin)
        db.refresh(op)
        db.refresh(viewer)

        pkg = SkillPackage(name=f"{P}pkg", display_name=f"{P}测试包", required_role="admin", is_active=True)
        db.add(pkg)
        db.commit()
        db.refresh(pkg)
        m1 = Skill(name=f"{P}pkg_m1", display_name="包成员1", required_role="admin",
                   is_active=True, package_id=pkg.id)
        m2 = Skill(name=f"{P}pkg_m2", display_name="包成员2", required_role="admin",
                   is_active=True, package_id=pkg.id)
        solo = Skill(name=f"{P}solo", display_name="独立技能", required_role="viewer",
                     is_active=True, package_id=None)
        db.add_all([m1, m2, solo])
        db.commit()
        for row in (pkg, m1, m2, solo):
            db.refresh(row)

        t_admin, t_op, t_viewer = login(admin.account), login(op.account), login(viewer.account)

        # T1 基线：公开技能按角色可见；admin 级对 operator 可见但锁定
        op_list = skill_names(t_op)
        check("T1 公开 viewer 技能 operator 可用", op_list.get(f"{P}solo", {}).get("has_access") is True)
        check("T1 admin 级技能 operator 可见但锁定",
              op_list.get(f"{P}pkg_m1", {}).get("has_access") is False)

        # T2 隐藏独立技能：operator 不可见，admin 可见
        r = httpx.put(f"{BASE}/skills/{solo.id}", headers=auth(t_admin),
                      json={"is_hidden": True}, timeout=15)
        r.raise_for_status()
        op_list, admin_list = skill_names(t_op), skill_names(t_admin)
        check("T2 隐藏后 operator 不可见", f"{P}solo" not in op_list)
        check("T2 隐藏后 admin 可见且带标记",
              admin_list.get(f"{P}solo", {}).get("is_hidden") is True)

        # T3 决策 B：隐藏技能角色达标（viewer 级 vs operator）也不可用
        check("T3 隐藏技能 operator 聊天白名单排除",
              f"{P}solo" not in usable_names(db, op.account))

        # T4 单技能授权：operator 恢复可见可用
        r = httpx.put(f"{BASE}/users/{op.id}", headers=auth(t_admin),
                      json={"skill_grant_skill_ids": [solo.id],
                            "skill_grant_package_ids": []}, timeout=15)
        r.raise_for_status()
        op_list = skill_names(t_op)
        check("T4 单技能授权后可见且可用",
              op_list.get(f"{P}solo", {}).get("has_access") is True)
        check("T4 授权进入聊天白名单", f"{P}solo" in usable_names(db, op.account))

        # T5 包隐藏：operator 看不到成员；viewer（未授权）同样看不到
        r = httpx.put(f"{BASE}/skills/packages/{pkg.id}", headers=auth(t_admin),
                      json={"is_hidden": True}, timeout=15)
        r.raise_for_status()
        op_list, viewer_list = skill_names(t_op), skill_names(t_viewer)
        check("T5 包隐藏后 operator 成员不可见", f"{P}pkg_m1" not in op_list)
        check("T5 包隐藏后 viewer 成员不可见", f"{P}pkg_m2" not in viewer_list)

        # T6 包授权 viewer：覆盖角色门槛 + 隐藏（决策 2/3）
        r = httpx.put(f"{BASE}/users/{viewer.id}", headers=auth(t_admin),
                      json={"skill_grant_package_ids": [pkg.id],
                            "skill_grant_skill_ids": []}, timeout=15)
        r.raise_for_status()
        viewer_list = skill_names(t_viewer)
        check("T6 包授权 viewer 成员可见",
              f"{P}pkg_m1" in viewer_list and f"{P}pkg_m2" in viewer_list)
        check("T6 包授权覆盖角色门槛（has_access）",
              viewer_list.get(f"{P}pkg_m1", {}).get("has_access") is True)
        check("T6 包授权进入聊天白名单",
              f"{P}pkg_m1" in usable_names(db, viewer.account))

        # T7 授权选项接口（人员编辑授权树数据）
        r = httpx.get(f"{BASE}/users/skill-grant-options", headers=auth(t_admin), timeout=15)
        r.raise_for_status()
        opts = r.json()
        check("T7 授权选项含测试包与技能",
              any(x["name"] == f"{P}pkg" for x in opts["packages"])
              and any(x["name"] == f"{P}solo" for x in opts["skills"]))

        # T8 移除授权：即时失效
        httpx.put(f"{BASE}/users/{op.id}", headers=auth(t_admin),
                  json={"skill_grant_skill_ids": []}, timeout=15).raise_for_status()
        httpx.put(f"{BASE}/users/{viewer.id}", headers=auth(t_admin),
                  json={"skill_grant_package_ids": []}, timeout=15).raise_for_status()
        op_list, viewer_list = skill_names(t_op), skill_names(t_viewer)
        check("T8 移除单技能授权后不可见", f"{P}solo" not in op_list)
        check("T8 移除包授权后成员不可见", f"{P}pkg_m1" not in viewer_list)
        check("T8 移除授权后聊天白名单排除",
              f"{P}solo" not in usable_names(db, op.account)
              and f"{P}pkg_m1" not in usable_names(db, viewer.account))
    finally:
        purge(db)
        db.close()

    failed = [row for row in results if not row[1]]
    print(f"\nSUMMARY: {len(results) - len(failed)}/{len(results)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
