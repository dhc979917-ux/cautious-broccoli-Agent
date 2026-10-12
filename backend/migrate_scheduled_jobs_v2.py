"""一次性迁移脚本：scheduled_jobs 表补列（v2：parent_id / deleted_at）。
parent_id：NULL=管理员创建的公共任务；非 NULL=用户选择公共任务生成的个人实例。
deleted_at：软删除标记（回收站）。
说明：调度任务模块在服务启动时也会经 ensure_tables() 自动补列（幂等），
本脚本为仓库约定的独立迁移入口，可在任意时刻重复执行。
用法（在 backend 目录下）：
    python migrate_scheduled_jobs_v2.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main():
    from app.services.scheduler_service import ensure_tables
    ensure_tables()
    from sqlalchemy import inspect
    from app.database import engine
    insp = inspect(engine)
    cols = [c["name"] for c in insp.get_columns("scheduled_jobs")]
    for need in ("parent_id", "deleted_at"):
        if need not in cols:
            raise SystemExit(f"[migrate_scheduled_jobs_v2] 缺列: {need}")
    print(f"[migrate_scheduled_jobs_v2] scheduled_jobs OK，列: {cols}")


if __name__ == "__main__":
    main()
