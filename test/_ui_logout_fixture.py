"""强制下线 + 调度任务"全部"分组的 UI 夹具。

造两样东西：
  1. 一个普通用户（供第二个浏览器上下文真实登录，验证被管理员改权限后强制下线）；
  2. 三条调度任务：管理员的公共任务、管理员的个人任务、上面那个用户从公共任务
     派生的个人实例——只有管理员的"全部"分组能同时看到这三条。

任务直接插库（enabled=False、next_run_at=NULL），不会被调度引擎触发；
之所以不走 POST /scheduled-jobs/，是因为该接口要求库里存在"任务助手"Agent，
本地开发库通常没有（与 test/_ui_fixtures.py 的处理方式一致）。

用法（conda env agentscope，任意目录）：
    python test/_ui_logout_fixture.py setup      # 写 test/_ui_logout_user.json
    python test/_ui_logout_fixture.py teardown   # 删除该用户及其授权行

口令写在生成的 json 里，只用于本地回归；账号名带 _UI_LOGOUT_ 前缀便于识别与清理。
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND_DIR = os.path.normpath(os.path.join(HERE, "..", "backend"))
os.chdir(BACKEND_DIR)
sys.path.insert(0, BACKEND_DIR)

from app.core.security import get_password_hash  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models.user import User  # noqa: E402
from app.models.user_agent_access import UserAgentAccess  # noqa: E402
from app.models.scheduled_job import ScheduledJob  # noqa: E402

ACCOUNT = "_UI_LOGOUT_tester"
PASSWORD = "TpUiTest#2026"
OUT = os.path.join(HERE, "_ui_logout_user.json")
JOB_PREFIX = "_UI_ALL_"
ADMIN_ID = 1     # 与 _ui_fixtures.py 一致：本地库的第一个管理员
AGENT_ID = 1     # 工具助手：任务只是列表展示用，不会被执行


def _purge(db):
    """删用户前先删授权行：留着会成为孤儿行，被复用同一 id 的下一个用户继承。"""
    db.query(ScheduledJob).filter(ScheduledJob.name.like(f"{JOB_PREFIX}%")).delete(synchronize_session=False)
    ids = [u.id for u in db.query(User).filter(User.account == ACCOUNT).all()]
    if ids:
        db.query(UserAgentAccess).filter(UserAgentAccess.user_id.in_(ids)).delete(synchronize_session=False)
        db.query(User).filter(User.id.in_(ids)).delete(synchronize_session=False)
        db.commit()


def setup():
    db = SessionLocal()
    try:
        _purge(db)
        user = User(
            account=ACCOUNT, username="_下线回归用户",
            password_hash=get_password_hash(PASSWORD),
            phone="18800000002", role="operator", is_active=True,
            permissions=None, token_version=0,
        )
        db.add(user)
        db.commit()
        db.refresh(user)

        def job(**kw):
            base = dict(
                agent_id=AGENT_ID, prompt="UI 回归夹具", trigger_type="cron",
                cron_expr="0 8 * * *", timezone="Asia/Shanghai",
                enabled=False, skip_if_running=False, timeout_minutes=60,
                max_retries=0, retry_interval_minutes=5, misfire_policy="run_once",
                notify_on="none", notify_channels=[], next_run_at=None,
            )
            base.update(kw)
            return ScheduledJob(**base)

        public = job(user_id=ADMIN_ID, parent_id=None, is_public=True,
                     name=JOB_PREFIX + "公共任务", description="公共任务")
        db.add(public)
        db.flush()
        personal = job(user_id=ADMIN_ID, parent_id=None, is_public=False,
                       name=JOB_PREFIX + "管理员个人任务", description="管理员个人任务")
        instance = job(user_id=user.id, parent_id=public.id, is_public=False,
                       name=JOB_PREFIX + "公共任务", description="他人实例")
        db.add_all([personal, instance])
        db.commit()

        with open(OUT, "w", encoding="utf-8") as fh:
            json.dump({
                "account": ACCOUNT, "password": PASSWORD, "user_id": user.id,
                "admin_id": ADMIN_ID,
                "jobs": {"public": public.id, "personal": personal.id, "instance": instance.id},
            }, fh, ensure_ascii=False)
        print(f"[OK] {OUT} (user={user.id} jobs=public:{public.id}/personal:{personal.id}/instance:{instance.id})")
    finally:
        db.close()


def teardown():
    db = SessionLocal()
    try:
        _purge(db)
        if os.path.isfile(OUT):
            os.remove(OUT)
        print("[OK] 下线回归账号已清理")
    finally:
        db.close()


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "setup"
    setup() if mode == "setup" else teardown()
