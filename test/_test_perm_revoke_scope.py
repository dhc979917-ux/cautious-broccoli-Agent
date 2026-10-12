"""权限即时生效 + 技能归属切换 回归验证（API 级，只用标准库）。

前置：后端已用当前代码启动在 :8000（conda env agentscope）。
用法（任意目录）：
    python test/_test_perm_revoke_scope.py

覆盖四组：
  1. 会话吊销：管理员改 角色/功能权限/Agent 授权/启用状态 -> 目标用户旧 token 立刻 401
     （detail 带 SESSION_REVOKED），重新登录后拿到新权限；只改邮箱等非权限字段不打扰用户。
  2. 强制停任务：进程内验证 task_manager.running_session_ids + stop_user_tasks + presence.push
     （等价用户按 Esc 的服务端版本）。
  3. 调度任务 scope=all：管理员看全量（含他人个人任务），普通用户 403，默认口径不变。
  4. 技能归属切换：可选 -> 工具助手内置 -> 可选 的剪切语义（目录真的移动、用户端列表消失、
     "刷新技能"不会把归属打回原形）、包成员脱离技能包、以及整包/重名的拒绝路径。

自带清理：临时用户与夹具任务删除，被移动的技能目录恢复原位并重新同步。
"""
import asyncio
import json
import os
import shutil
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND_DIR = os.path.normpath(os.path.join(HERE, "..", "backend"))
os.chdir(BACKEND_DIR)
sys.path.insert(0, BACKEND_DIR)

from app.database import SessionLocal  # noqa: E402
from app.core.security import create_access_token, get_password_hash  # noqa: E402
from app.models.scheduled_job import ScheduledJob  # noqa: E402
from app.models.skill import Skill  # noqa: E402
from app.models.user import User  # noqa: E402
from app.models.user_agent_access import UserAgentAccess  # noqa: E402
from app.services.skill_service import SkillService  # noqa: E402

BASE = f"http://127.0.0.1:{os.environ.get('TP_PORT', '8000')}/api/v1"
TEST_ACCOUNT = "_perm_revoke_test"
TEST_PASSWORD = "TpTest#2026"

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print(f"{'PASS' if ok else 'FAIL'} - {name}" + (f" | {detail}" if detail else ""))


# ---------------- HTTP ----------------

def call(method, path, token=None, body=None):
    """返回 (status_code, parsed_json_or_text)。不抛异常，便于断言错误码。"""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            raw = r.read().decode("utf-8")
            return r.status, _parse(raw)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        return e.code, _parse(raw)


def _parse(raw):
    try:
        return json.loads(raw)
    except Exception:
        return raw


def login(account, password):
    status, body = call("POST", "/auth/login", body={"account": account, "password": password})
    if status != 200:
        raise SystemExit(f"[FATAL] 测试账号登录失败 {status}: {body}")
    return body["access_token"], body["user"]


def admin_token(db):
    admin = db.query(User).filter(User.role == "admin", User.is_active == True).order_by(User.id).first()  # noqa: E712
    if not admin:
        raise SystemExit("[FATAL] 库里没有可用的管理员账号")
    # 直接签发（带上当前 token_version），不依赖管理员口令
    return admin.id, create_access_token({"sub": str(admin.id), "tv": int(admin.token_version or 0)})


# ---------------- 夹具 ----------------

def setup_user(db):
    # 连授权行一起清：只删 users 会留下孤儿 user_agent_access，
    # 新用户复用同一个 id 时会"继承"上一次的 Agent 授权，导致授权变更断言失效
    stale = [u.id for u in db.query(User).filter(User.account == TEST_ACCOUNT).all()]
    if stale:
        db.query(UserAgentAccess).filter(UserAgentAccess.user_id.in_(stale)).delete(synchronize_session=False)
        db.query(User).filter(User.id.in_(stale)).delete(synchronize_session=False)
        db.commit()
    user = User(
        account=TEST_ACCOUNT, username="_权限回归用户",
        password_hash=get_password_hash(TEST_PASSWORD),
        phone="18800000000", role="operator", is_active=True,
        permissions=None, token_version=0,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user.id


def setup_job(db, owner_id):
    """他人个人任务：默认口径下管理员也看不到，scope=all 才出现。"""
    db.query(ScheduledJob).filter(ScheduledJob.name == "_PERM_TEST_他人个人任务").delete()
    job = ScheduledJob(
        user_id=owner_id, agent_id=1, name="_PERM_TEST_他人个人任务",
        prompt="回归夹具", trigger_type="cron", cron_expr="0 8 * * *",
        timezone="Asia/Shanghai", is_public=False, enabled=False,
        skip_if_running=False, timeout_minutes=60, max_retries=0,
        retry_interval_minutes=5, misfire_policy="run_once",
        notify_on="none", notify_channels=[], next_run_at=None,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job.id


def teardown(db, user_id, job_id, restores):
    """清理：恢复被移动的技能目录 -> 删夹具任务 -> 删测试用户。"""
    for skill_id, original_rel in restores:
        try:
            _restore_skill(skill_id, original_rel)
        except Exception as e:
            print(f"[WARN] 技能 {skill_id} 目录恢复失败，请手工检查: {e}")
    if job_id:
        db.query(ScheduledJob).filter(ScheduledJob.id == job_id).delete()
    if user_id:
        db.query(UserAgentAccess).filter(UserAgentAccess.user_id == user_id).delete(synchronize_session=False)
    db.query(User).filter(User.id == user_id).delete()
    db.commit()


def _restore_skill(skill_id, original_rel):
    """把技能目录移回原 rel_path 并重新同步（归属切换测试的回滚）。"""
    db = SessionLocal()
    try:
        skill = db.query(Skill).filter(Skill.id == skill_id).first()
        if not skill or skill.rel_path == original_rel:
            return
        cur = SkillService._abs_from_rel(skill.rel_path)
        dst = SkillService._abs_from_rel(original_rel)
        if cur and dst and os.path.isdir(cur) and not os.path.exists(dst):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.move(cur, dst)
            # 归属锁会阻止同步改回标志，先解锁再同步
            skill.scope_locked = False
            db.commit()
            SkillService(db).sync_from_filesystem()
            print(f"[OK] 已恢复技能目录: {skill.name} -> {original_rel}")
    finally:
        db.close()


# ---------------- 1. 会话吊销 ----------------

def test_revoke(db, user_id, admin_id, atoken):
    print("\n=== 1. 权限变更即时生效（旧 token 立刻失效） ===")
    tok, me = login(TEST_ACCOUNT, TEST_PASSWORD)
    check("登录后 /auth/me 可用", call("GET", "/auth/me", tok)[0] == 200, f"user={me['username']}")
    check("初始无附加权限", me.get("permissions") == [], str(me.get("permissions")))

    # 改功能权限 -> 旧 token 立刻失效
    status, _ = call("PUT", f"/users/{user_id}", atoken, {"permissions": ["mcp_manage"]})
    check("管理员改功能权限返回 200", status == 200, str(status))
    status, body = call("GET", "/auth/me", tok)
    detail = body.get("detail", "") if isinstance(body, dict) else str(body)
    check("旧 token 立刻 401", status == 401, str(status))
    check("401 带 SESSION_REVOKED 标识", "SESSION_REVOKED" in detail, detail)

    tok2, me2 = login(TEST_ACCOUNT, TEST_PASSWORD)
    check("重新登录后新权限生效", me2.get("permissions") == ["mcp_manage"], str(me2.get("permissions")))

    # 只改邮箱：不该踢人
    status, _ = call("PUT", f"/users/{user_id}", atoken, {"email": "perm_test@example.com"})
    check("只改邮箱返回 200", status == 200, str(status))
    check("非权限字段变更不吊销会话", call("GET", "/auth/me", tok2)[0] == 200)

    # 改角色 -> 吊销
    call("PUT", f"/users/{user_id}", atoken, {"role": "viewer"})
    check("改角色后旧 token 失效", call("GET", "/auth/me", tok2)[0] == 401)
    tok3, me3 = login(TEST_ACCOUNT, TEST_PASSWORD)
    check("重新登录后角色已更新", me3.get("role") == "viewer", str(me3.get("role")))

    # 改 Agent 授权 -> 吊销（用与当前不同的集合，避免"其实没变化"造成误判）
    current_agents = sorted(int(x) for x in (me3.get("agent_permissions") or []))
    call("PUT", f"/users/{user_id}", atoken, {"agent_ids": [] if current_agents else [1]})
    check("改 Agent 授权后旧 token 失效", call("GET", "/auth/me", tok3)[0] == 401)
    tok4, _ = login(TEST_ACCOUNT, TEST_PASSWORD)

    # 禁用账号 -> 吊销 + 无法登录
    call("PUT", f"/users/{user_id}", atoken, {"is_active": False})
    check("禁用后旧 token 失效", call("GET", "/auth/me", tok4)[0] in (401, 403))
    status, _ = call("POST", "/auth/login", body={"account": TEST_ACCOUNT, "password": TEST_PASSWORD})
    check("禁用后无法登录", status == 401, str(status))
    call("PUT", f"/users/{user_id}", atoken, {"is_active": True, "role": "operator"})
    check("恢复启用后可重新登录", login(TEST_ACCOUNT, TEST_PASSWORD)[1]["role"] == "operator")

    # 管理员改自己：不允许自我降级（既有保护不能被破坏）
    status, body = call("PUT", f"/users/{admin_id}", atoken, {"role": "operator"})
    detail = body.get("detail", "") if isinstance(body, dict) else str(body)
    check("管理员不能降级自己", status == 400, detail)


# ---------------- 2. 强制停任务 + 在线推送 ----------------

async def test_force_stop():
    print("\n=== 2. 服务端强制停任务（等价 Esc） ===")
    from app.core import presence
    from app.services.task_control import REASON_ADMIN_REVOKE, stop_user_tasks
    from app.services.task_manager import task_manager

    fake_user = 999999
    sent = []

    class FakeWs:
        async def send_json(self, payload):
            sent.append(payload)

        async def close(self, code=None, reason=None):
            sent.append({"closed": code})

    async def worker(emit):
        await asyncio.sleep(60)
        await emit({"type": "end"})

    info = task_manager.start(987654, worker)
    info.user_id = fake_user
    check("任务已登记为运行中", task_manager.is_running(987654))
    check("按用户能查到运行中的会话", task_manager.running_session_ids(fake_user) == [987654],
          str(task_manager.running_session_ids(fake_user)))

    ws = FakeWs()
    presence.register(fake_user, ws)
    stopped = await stop_user_tasks(fake_user, REASON_ADMIN_REVOKE)
    check("强制停止命中该会话", stopped == [987654], str(stopped))
    check("任务已到终态 stopped", task_manager.get(987654).status == "stopped",
          task_manager.get(987654).status)
    check("停止原因记为 admin_revoked", task_manager.get(987654).stop_reason == REASON_ADMIN_REVOKE)

    delivered = await presence.push(fake_user, {"type": "force_logout", "reason": "permissions_changed"},
                                    close_code=4001, close_reason="登录状态已失效")
    check("在线连接收到推送", delivered == 1 and sent[0]["type"] == "force_logout", str(sent))
    check("推送后连接被关闭", any(item.get("closed") == 4001 for item in sent), str(sent))
    check("推送后连接已注销", not presence.is_online(fake_user))


# ---------------- 3. 调度任务 scope=all ----------------

def test_jobs(db, user_id, atoken, job_id, job_owner):
    print("\n=== 3. 调度任务 全部 分组 ===")
    status, all_jobs = call("GET", "/scheduled-jobs/?scope=all", atoken)
    check("管理员 scope=all 返回 200", status == 200, str(status))
    ids = [j["id"] for j in all_jobs] if isinstance(all_jobs, list) else []
    check("scope=all 含他人个人任务", job_id in ids, f"job={job_id} in {ids}")

    status, default_jobs = call("GET", "/scheduled-jobs/", atoken)
    default_ids = [j["id"] for j in default_jobs] if isinstance(default_jobs, list) else []
    check("默认口径仍看不到他人个人任务", job_id not in default_ids, str(default_ids))

    utok, _ = login(TEST_ACCOUNT, TEST_PASSWORD)
    status, body = call("GET", "/scheduled-jobs/?scope=all", utok)
    check("普通用户 scope=all 被拒 403", status == 403, str(status))
    status, mine = call("GET", "/scheduled-jobs/", utok)
    mine_ids = [j["id"] for j in mine] if isinstance(mine, list) else []
    if job_owner == user_id:
        check("普通用户默认口径不含他人任务", True, "库里没有第三个可用用户，跳过")
    else:
        check("普通用户默认口径不含他人任务", status == 200 and job_id not in mine_ids, str(mine_ids))

    row = next((j for j in all_jobs if j["id"] == job_id), None) if isinstance(all_jobs, list) else None
    check("scope=all 的行带属主名", bool(row and row.get("owner_name")), str(row and row.get("owner_name")))


# ---------------- 4. 技能归属切换 ----------------

def _pick_standalone(db):
    """可选根下的独立技能（不属于包、目录里没有嵌套技能）。"""
    svc = SkillService(db)
    rows = (db.query(Skill)
            .filter(Skill.is_active == True, Skill.package_id.is_(None),  # noqa: E712
                    Skill.builtin_dialog == False, Skill.builtin_task == False)  # noqa: E712
            .all())
    for s in rows:
        d = svc._abs_from_rel(s.rel_path)
        if d and os.path.isdir(d) and not svc._nested_skill_dirs(d):
            return s
    return None


def _pick_package_member(db):
    """技能包里的一个叶子成员（用于验证"成员切换归属 = 脱离技能包"）。"""
    svc = SkillService(db)
    rows = (db.query(Skill)
            .filter(Skill.is_active == True, Skill.package_id.isnot(None),  # noqa: E712
                    Skill.builtin_dialog == False, Skill.builtin_task == False)  # noqa: E712
            .all())
    for s in rows:
        d = svc._abs_from_rel(s.rel_path)
        if d and os.path.isdir(d) and not svc._nested_skill_dirs(d):
            return s
    return None


def _pick_nested(db):
    """目录里还嵌着其它技能的行（技能包本体）：应被拒绝整体移动。"""
    svc = SkillService(db)
    for s in db.query(Skill).filter(Skill.is_active == True).all():  # noqa: E712
        d = svc._abs_from_rel(s.rel_path)
        if d and os.path.isdir(d) and svc._nested_skill_dirs(d):
            return s
    return None


def _user_visible_names(tok):
    status, body = call("GET", "/skills/", tok)
    if status != 200 or not isinstance(body, list):
        return set()
    return {item["name"] for item in body}


def test_skill_scope(db, atoken, restores):
    print("\n=== 4. 技能归属切换（可选 <-> 内置） ===")
    utok, _ = login(TEST_ACCOUNT, TEST_PASSWORD)
    agents_dir = SkillService._agents_dir()

    standalone = _pick_standalone(db)
    if not standalone:
        check("找到可切换的独立技能", False, "可选根下没有独立技能，跳过")
    else:
        sid, name = standalone.id, standalone.name
        original_rel = standalone.rel_path
        restores.append((sid, original_rel))
        before_abs = SkillService._abs_from_rel(original_rel)
        visible_before = _user_visible_names(utok)
        check("切换前用户端可见", name in visible_before, name)

        status, body = call("PUT", f"/skills/{sid}/scope", atoken, {"scope": "dialog"})
        check("内置到工具助手返回 200", status == 200, str(body if status != 200 else ""))
        db.expire_all()
        row = db.query(Skill).filter(Skill.id == sid).first()
        check("标志位 builtin_dialog=1", int(bool(row.builtin_dialog)) == 1, str(row.builtin_dialog))
        check("归属已锁定 scope_locked=1", bool(row.scope_locked))
        check("rel_path 落到内置根", str(row.rel_path).startswith("skills_dialog/"), str(row.rel_path))
        new_abs = SkillService._abs_from_rel(row.rel_path)
        check("目录真的移动了", os.path.isdir(new_abs) and not os.path.exists(before_abs),
              f"{before_abs} -> {new_abs}")
        check("用户端不再可见（剪切语义）", name not in _user_visible_names(utok), name)

        status, body = call("POST", "/skills/refresh", atoken)
        db.expire_all()
        row = db.query(Skill).filter(Skill.id == sid).first()
        check("刷新技能不会把归属打回原形",
              status == 200 and int(bool(row.builtin_dialog)) == 1 and str(row.rel_path).startswith("skills_dialog/"),
              f"status={status} rel={row.rel_path}")

        status, _ = call("PUT", f"/skills/{sid}/scope", atoken, {"scope": "task"})
        db.expire_all()
        row = db.query(Skill).filter(Skill.id == sid).first()
        check("内置归属之间可互切（dialog -> task）",
              status == 200 and int(bool(row.builtin_task)) == 1 and int(bool(row.builtin_dialog)) == 0,
              str(row.rel_path))

        status, _ = call("PUT", f"/skills/{sid}/scope", atoken, {"scope": "optional"})
        db.expire_all()
        row = db.query(Skill).filter(Skill.id == sid).first()
        back_abs = SkillService._abs_from_rel(row.rel_path)
        check("取消内置回到可选根",
              status == 200 and not row.builtin_dialog and not row.builtin_task
              and str(row.rel_path).startswith("skills/") and os.path.isdir(back_abs),
              str(row.rel_path))
        check("回到可选后用户端重新可见", name in _user_visible_names(utok), name)

        # 幂等：目标归属与当前一致时不应报错、也不该动磁盘
        status, _ = call("PUT", f"/skills/{sid}/scope", atoken, {"scope": "optional"})
        check("重复切换到同一归属幂等", status == 200, str(status))

    # 拒绝路径：整包（目录里还有嵌套技能）
    nested = _pick_nested(db)
    if nested:
        status, body = call("PUT", f"/skills/{nested.id}/scope", atoken, {"scope": "dialog"})
        detail = body.get("detail", "") if isinstance(body, dict) else str(body)
        check("技能包整体切换被拒 400", status == 400 and "嵌套技能" in detail, detail)
    else:
        check("技能包整体切换被拒 400", False, "没有可用于验证的嵌套技能目录")

    # 非法取值
    if standalone:
        status, body = call("PUT", f"/skills/{standalone.id}/scope", atoken, {"scope": "nowhere"})
        detail = body.get("detail", "") if isinstance(body, dict) else str(body)
        check("非法归属取值被拒 400", status == 400 and "归属只能是" in detail, detail)

    # 包成员：允许切换，切换后脱离技能包
    member = _pick_package_member(db)
    if member:
        mid, mname = member.id, member.name
        original_rel = member.rel_path
        pkg_id_before = member.package_id
        restores.append((mid, original_rel))
        status, body = call("PUT", f"/skills/{mid}/scope", atoken, {"scope": "dialog"})
        db.expire_all()
        row = db.query(Skill).filter(Skill.id == mid).first()
        check("包成员可内置", status == 200 and str(row.rel_path).startswith("skills_dialog/"),
              str(body if status != 200 else row.rel_path))
        check("包成员内置后脱离技能包", row.package_id is None, f"before={pkg_id_before} after={row.package_id}")
    else:
        check("包成员可内置", False, "没有可用于验证的技能包成员")

    # 播种抑制名单：被移出内置根的技能要记进 .deleted_skills，避免 entrypoint 回灌
    tracker = os.path.join(agents_dir, "skills_dialog", ".deleted_skills")
    names = set()
    if os.path.isfile(tracker):
        with open(tracker, "r", encoding="utf-8") as fh:
            names = {line.strip() for line in fh if line.strip()}
    # 上面把独立技能从 dialog/task 取消内置回可选根，名字应记进 dialog 根的抑制名单，
    # 否则容器重启时 entrypoint 会用镜像种子把它补回内置根（"过一天又恢复"的根因）
    check("取消内置后写入播种抑制名单",
          bool(standalone and standalone.name in names) or bool(member and member.name in names),
          f"tracker={sorted(names)[:6]}")


def main():
    db = SessionLocal()
    restores = []
    user_id = job_id = None
    try:
        user_id = setup_user(db)
        admin_id, atoken = admin_token(db)
        # 夹具任务必须挂在"既不是管理员、也不是测试用户"的名下，
        # 才能验证 默认口径看不到他人任务 / scope=all 看得到
        other = (db.query(User)
                 .filter(User.id != admin_id, User.id != user_id, User.is_active == True)  # noqa: E712
                 .order_by(User.id).first())
        job_owner = other.id if other else user_id
        job_id = setup_job(db, job_owner)

        test_revoke(db, user_id, admin_id, atoken)
        asyncio.run(test_force_stop())
        test_jobs(db, user_id, atoken, job_id, job_owner)
        test_skill_scope(db, atoken, restores)
    finally:
        try:
            teardown(db, user_id, job_id, restores)
        finally:
            db.close()

    passed = sum(1 for _n, ok in results if ok)
    print(f"\n==== {passed}/{len(results)} 项通过 ====")
    for name, ok in results:
        if not ok:
            print(f"  FAILED: {name}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
