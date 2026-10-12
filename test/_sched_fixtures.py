"""调度任务模块 E2E 夹具：创建/清理临时管理员、普通用户与"任务助手"Agent。

用法（conda env agentscope，任意目录均可执行，脚本会自动切到 backend 目录）：
    python test/_sched_fixtures.py setup
    python test/_sched_fixtures.py teardown

凭据（可被环境变量覆盖）：
    SCHED_TMP_ADMIN / SCHED_TMP_ADMIN_PWD  默认 _sched_tmp_admin / tmpAdmin123!
    SCHED_TMP_USER  / SCHED_TMP_USER_PWD   默认 _sched_tmp_user  / tmpUser123!
"""
import os
import sys

def _resolve_backend_dir():
    # 1) 环境变量显式指定（容器内用：SCHED_BACKEND_DIR=/app）
    env_dir = os.environ.get("SCHED_BACKEND_DIR")
    if env_dir:
        return os.path.abspath(env_dir)
    # 2) 仓库布局：test/ 的同级 backend/
    here = os.path.dirname(os.path.abspath(__file__))
    candidate = os.path.normpath(os.path.join(here, "..", "backend"))
    if os.path.isdir(os.path.join(candidate, "app")):
        return candidate
    # 3) 兜底：当前工作目录包含 app 包（如被复制进容器任意位置运行）
    if os.path.isdir(os.path.join(os.getcwd(), "app")):
        return os.getcwd()
    return candidate

BACKEND_DIR = _resolve_backend_dir()
os.chdir(BACKEND_DIR)
sys.path.insert(0, BACKEND_DIR)

ADMIN_NAME = os.environ.get("SCHED_TMP_ADMIN", "_sched_tmp_admin")
ADMIN_PWD = os.environ.get("SCHED_TMP_ADMIN_PWD", "tmpAdmin123!")
USER_NAME = os.environ.get("SCHED_TMP_USER", "_sched_tmp_user")
USER_PWD = os.environ.get("SCHED_TMP_USER_PWD", "tmpUser123!")
AGENT_NAME = os.environ.get("SCHEDULER_TASK_AGENT_NAME", "任务助手")
AGENT_MARK = "sched-tmp-fixture"


def _ensure_user(db, User, get_password_hash, name, pwd, role):
    u = db.query(User).filter(User.username == name).first()
    if u:
        # 保证角色/密码/启用状态与测试预期一致（幂等）
        u.role = role
        u.is_active = True
        u.password_hash = get_password_hash(pwd)
        if not u.account:
            u.account = name
        db.commit()
        print(f"[fixtures] 用户已存在并已对齐: {name} (id={u.id}, role={role})")
        return u.id
    u = User(
        username=name, account=name,
        password_hash=get_password_hash(pwd),
        email=f"{name.strip('_')}@example.com",
        phone="18800000000",
        role=role, is_active=True,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    print(f"[fixtures] 已创建用户: {name} (id={u.id}, role={role})")
    return u.id


def setup():
    from app.database import SessionLocal
    from app.core.security import get_password_hash
    from app.models.agent import Agent
    from app.models.user import User

    db = SessionLocal()
    try:
        _ensure_user(db, User, get_password_hash, ADMIN_NAME, ADMIN_PWD, "admin")
        _ensure_user(db, User, get_password_hash, USER_NAME, USER_PWD, "operator")

        agent = db.query(Agent).filter(Agent.name == AGENT_NAME).first()
        if agent:
            if not agent.is_active:
                agent.is_active = True
                db.commit()
            print(f"[fixtures] {AGENT_NAME} Agent 已存在 (id={agent.id})，沿用")
        else:
            agent = Agent(
                name=AGENT_NAME,
                description=f"{AGENT_MARK}：调度任务模块测试用任务助手",
                agent_type="dialog",
                model_config_name="qwen-max",
                system_prompt=(
                    "你是任务助手，负责按计划自动完成用户交办的事务性任务。"
                    "请直接用中文完成指令；如无必要不要调用任何工具。"
                ),
                is_active=True,
            )
            db.add(agent)
            db.commit()
            db.refresh(agent)
            print(f"[fixtures] 已创建 {AGENT_NAME} Agent (id={agent.id})")
    finally:
        db.close()


def _purge_user_refs(db, uids):
    """级联删除所有通过外键引用 users 的表中的临时用户数据。
    平台会在用户登录/浏览时自动创建知识库、文件等数据，直接删用户会撞外键。
    这里按外键图做传播式清理：从 users 出发，逐轮删除引用了"已删主键"的行，
    直到收敛。仅影响临时用户名下的数据（按外键值过滤）。"""
    from sqlalchemy import inspect, text
    from app.database import engine
    insp = inspect(engine)
    # table -> [(fk 列, 被引用表)]
    edges = {}
    for t in insp.get_table_names():
        try:
            for fk in insp.get_foreign_keys(t):
                edges.setdefault(t, []).append(
                    (fk["constrained_columns"], fk["referred_table"]))
        except Exception:
            continue
    deleted = {"users": set(int(i) for i in uids)}
    for _ in range(25):  # 逐轮传播，处理多级依赖；封顶防死循环
        progress = False
        for t, fks in edges.items():
            if t == "users":
                continue
            pkc = (insp.get_pk_constraint(t) or {}).get("constrained_columns") or []
            if len(pkc) != 1:
                continue
            pk = pkc[0]
            for cols, referred in fks:
                if referred not in deleted or not deleted[referred] or len(cols) != 1:
                    continue
                col = cols[0]
                ph = ",".join(str(int(i)) for i in sorted(deleted[referred]))
                rows = db.execute(
                    text(f'SELECT "{pk}" FROM "{t}" WHERE "{col}" IN ({ph})')).fetchall()
                if not rows:
                    continue
                db.execute(text(f'DELETE FROM "{t}" WHERE "{col}" IN ({ph})'))
                deleted.setdefault(t, set()).update(r[0] for r in rows)
                progress = True
        if not progress:
            break
    db.flush()


def teardown():
    from app.database import SessionLocal
    from app.models.agent import Agent
    from app.models.notification import Notification
    from app.models.scheduled_job import ScheduledJob, ScheduledJobRun
    from app.models.session import Session as ChatSession
    from app.models.message import Message
    from app.models.user import User
    from app.models.user_agent_access import UserAgentAccess

    db = SessionLocal()
    try:
        users = db.query(User).filter(User.username.in_([ADMIN_NAME, USER_NAME])).all()
        uids = [u.id for u in users]

        if uids:
            jobs = db.query(ScheduledJob).filter(ScheduledJob.user_id.in_(uids)).all()
            jids = [j.id for j in jobs]
            if jids:
                (db.query(ScheduledJobRun)
                   .filter(ScheduledJobRun.job_id.in_(jids))
                   .delete(synchronize_session=False))
                (db.query(ScheduledJob)
                   .filter(ScheduledJob.id.in_(jids))
                   .delete(synchronize_session=False))

            sessions = db.query(ChatSession).filter(ChatSession.user_id.in_(uids)).all()
            sids = [s.id for s in sessions]
            if sids:
                (db.query(Message)
                   .filter(Message.session_id.in_(sids))
                   .delete(synchronize_session=False))
                (db.query(ChatSession)
                   .filter(ChatSession.id.in_(sids))
                   .delete(synchronize_session=False))

            (db.query(Notification)
               .filter(Notification.user_id.in_(uids))
               .delete(synchronize_session=False))
            (db.query(UserAgentAccess)
               .filter(UserAgentAccess.user_id.in_(uids))
               .delete(synchronize_session=False))
            # 级联清理其余引用 users 的表（知识库/文件等登录时自动创建的数据）
            _purge_user_refs(db, uids)
            (db.query(User)
               .filter(User.id.in_(uids))
               .delete(synchronize_session=False))
            print(f"[fixtures] 已清理临时用户及其数据: {uids}")

        agent = db.query(Agent).filter(Agent.name == AGENT_NAME).first()
        if agent and agent.description and AGENT_MARK in agent.description:
            (db.query(UserAgentAccess)
               .filter(UserAgentAccess.agent_id == agent.id)
               .delete(synchronize_session=False))
            db.delete(agent)
            print(f"[fixtures] 已清理临时 {AGENT_NAME} Agent (id={agent.id})")
        elif agent:
            print(f"[fixtures] {AGENT_NAME} Agent 非夹具创建，保留")
        db.commit()
    finally:
        db.close()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "setup"
    if cmd == "setup":
        setup()
    elif cmd == "teardown":
        teardown()
    else:
        raise SystemExit(f"未知命令: {cmd}（可选 setup / teardown）")
