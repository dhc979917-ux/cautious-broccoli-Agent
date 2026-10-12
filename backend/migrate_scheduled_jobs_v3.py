"""一次性迁移脚本：scheduled_jobs 表补列（v3：is_public）。
is_public：True=公共任务（全员可见、可被"选择"）；False=个人任务/用户实例（仅属主可见）。
存量回填：parent_id 非空的行（用户实例）置 FALSE，其余保持默认 TRUE。
说明：调度任务模块在服务启动时也会经 ensure_tables() 自动补列并回填（幂等），
本脚本为仓库约定的独立迁移入口，可在任意时刻重复执行。
SQLite 与 Postgres 两种方言均可运行。
用法（在 backend 目录下）：
    python migrate_scheduled_jobs_v3.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main():
    from app.services.scheduler_service import ensure_tables
    ensure_tables()
    from sqlalchemy import inspect, text
    from app.database import engine
    insp = inspect(engine)
    cols = [c["name"] for c in insp.get_columns("scheduled_jobs")]
    for need in ("parent_id", "deleted_at", "is_public"):
        if need not in cols:
            raise SystemExit(f"[migrate_scheduled_jobs_v3] 缺列: {need}")
    with engine.begin() as conn:
        n = conn.execute(text(
            "UPDATE scheduled_jobs SET is_public = FALSE "
            "WHERE parent_id IS NOT NULL AND is_public")).rowcount or 0
    print(f"[migrate_scheduled_jobs_v3] scheduled_jobs OK，列: {cols}")
    print(f"[migrate_scheduled_jobs_v3] 本次回填 is_public=FALSE 共 {n} 行")


if __name__ == "__main__":
    main()
