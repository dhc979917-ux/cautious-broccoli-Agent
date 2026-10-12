"""文件中心 / 调度任务 UI 回归夹具：造一个公共任务 + 他人实例 + 执行记录。

用法（conda env agentscope，任意目录执行，脚本自动切到 backend）：
    python test/_ui_fixtures.py setup
    python test/_ui_fixtures.py teardown
    python test/_ui_fixtures.py auth [user_id]   # 生成免密登录用的 localStorage 快照

只插入 enabled=False、next_run_at=NULL 的任务，不会被调度引擎触发；
名称统一带 _UI_TEST_ 前缀，teardown 按前缀清理，幂等可重复执行。
"""
import os
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND_DIR = os.path.normpath(os.path.join(HERE, "..", "backend"))
os.chdir(BACKEND_DIR)
sys.path.insert(0, BACKEND_DIR)

from app.database import SessionLocal  # noqa: E402
from app.models.scheduled_job import ScheduledJob, ScheduledJobRun  # noqa: E402
from app.models.file import FileRecord  # noqa: E402
from app.services.session_service import SessionService  # noqa: E402

PREFIX = "_UI_TEST_"
ADMIN_ID = 1     # 石少毅（admin）：公共任务创建者
OTHER_ID = 8     # 张烨（admin）：实例属主，用于验证"他人执行记录"
AGENT_ID = 1     # 工具助手：查看会话跳转的目标 Agent
ADMIN_SESSION = 94   # 属 ADMIN_ID、agent=1 的真实会话
OTHER_SESSION = 78   # 属 OTHER_ID、agent=1 的真实会话


def _job(**kw) -> ScheduledJob:
    base = dict(
        agent_id=AGENT_ID, prompt="UI 回归测试指令", trigger_type="cron",
        cron_expr="0 8 * * *", timezone="Asia/Shanghai",
        enabled=False, skip_if_running=False, timeout_minutes=60,
        max_retries=0, retry_interval_minutes=5, misfire_policy="run_once",
        notify_on="none", notify_channels=[], next_run_at=None,
    )
    base.update(kw)
    return ScheduledJob(**base)


def setup():
    db = SessionLocal()
    try:
        teardown(db, quiet=True)
        public = _job(user_id=ADMIN_ID, parent_id=None, is_public=True,
                      name=PREFIX + "公共任务", description="公共任务")
        db.add(public)
        db.flush()
        instance = _job(user_id=OTHER_ID, parent_id=public.id, is_public=False,
                        name=PREFIX + "公共任务", description="他人实例")
        private = _job(user_id=OTHER_ID, parent_id=None, is_public=False,
                       name=PREFIX + "他人个人任务", description="管理员不应看到")
        db.add_all([instance, private])
        db.flush()

        day = datetime(2026, 9, 8, 8, 0, 0)
        runs = [
            # 公共任务本人的一次成功执行：耗时 11分40秒
            ScheduledJobRun(job_id=public.id, session_id=ADMIN_SESSION, trigger_type="cron",
                            attempt=1, status="success", started_at=day,
                            finished_at=datetime(2026, 9, 8, 8, 11, 40),
                            reply_summary="这段AI回复摘要不应出现在执行历史里"),
            # 他人实例：成功 + 失败（失败原因收进状态标签的悬浮提示，不占行）
            ScheduledJobRun(job_id=instance.id, session_id=OTHER_SESSION, trigger_type="cron",
                            attempt=1, status="success", started_at=day,
                            finished_at=datetime(2026, 9, 8, 8, 9, 5)),
            ScheduledJobRun(job_id=instance.id, session_id=None, trigger_type="cron",
                            attempt=1, status="failed", error="Agent 执行超时（夹具）",
                            started_at=day, finished_at=datetime(2026, 9, 8, 8, 20, 0)),
        ]
        db.add_all(runs)
        db.commit()
        print(f"[OK] public_job={public.id} instance={instance.id} other_private={private.id}")
        print(f"[OK] runs={[r.id for r in runs]}")

        # 来源会话已删除的产出文件：走真实删除链路，验证标题快照保留 + 前端置灰
        svc = SessionService(db)
        sess = svc.create_session(user_id=ADMIN_ID, agent_id=AGENT_ID,
                                 title=PREFIX + "将被删除的会话")
        rec = FileRecord(user_id=ADMIN_ID, kind="output",
                         original_name=PREFIX + "已删会话产出.csv",
                         stored_name="_ui_test_deleted.csv", size=1234,
                         session_id=sess.id, agent_id=AGENT_ID,
                         session_title=sess.title)
        db.add(rec)
        db.commit()
        svc.delete_session(sess.id, ADMIN_ID)
        db.refresh(rec)
        print(f"[OK] deleted-session file={rec.id} session_id={rec.session_id} "
              f"title={rec.session_title}")
    finally:
        db.close()


def teardown(db=None, quiet=False):
    own = db is None
    db = db or SessionLocal()
    try:
        job_ids = [j.id for j in db.query(ScheduledJob)
                   .filter(ScheduledJob.name.like(PREFIX + "%")).all()]
        if job_ids:
            db.query(ScheduledJobRun).filter(
                ScheduledJobRun.job_id.in_(job_ids)).delete(synchronize_session=False)
            db.query(ScheduledJob).filter(
                ScheduledJob.id.in_(job_ids)).delete(synchronize_session=False)
            db.commit()
        files = db.query(FileRecord).filter(
            FileRecord.original_name.like(PREFIX + "%")).delete(synchronize_session=False)
        db.commit()
        if not quiet:
            print(f"[OK] 已清理夹具任务 {len(job_ids)} 个、夹具文件 {files} 个")
    finally:
        if own:
            db.close()


def write_auth(user_id: int = ADMIN_ID):
    """生成前端 localStorage 快照（auth-storage），Playwright 注入后免密登录。
    selectedAgentId 固定为工具助手，复现"主页选了工具助手再跨 Agent 跳转"的场景。"""
    import json
    import urllib.request

    from app.core.security import create_access_token
    from app.database import SessionLocal
    from app.models.user import User

    # 带上当前 token_version：管理员改过该用户的角色/权限后 users.token_version 会自增，
    # 缺少 tv claim 的令牌会被 core/deps.py 判为已吊销（401），夹具就登录不进去了
    with SessionLocal() as db:
        row = db.query(User).filter(User.id == user_id).first()
        tv = int(row.token_version or 0) if row else 0
    token = create_access_token({"sub": str(user_id), "tv": tv})
    req = urllib.request.Request(
        f"http://127.0.0.1:{os.environ.get('TP_PORT', '8000')}/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=20) as r:
        me = json.loads(r.read().decode("utf-8"))
    payload = {
        "state": {
            "token": token, "user": me, "theme": "light", "motionEffect": False,
            "selectedAgentId": AGENT_ID, "selectedSkills": [], "selectedMcps": None,
        },
        "version": 6,
    }
    out = os.path.join(HERE, "_ui_auth.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)
    print(f"[OK] {out} (user={me.get('username')} id={user_id} selectedAgentId={AGENT_ID})")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "setup"
    if mode == "setup":
        setup()
    elif mode == "auth":
        write_auth(int(sys.argv[2]) if len(sys.argv) > 2 else ADMIN_ID)
    else:
        teardown()
