"""一次性迁移脚本：创建 scheduled_jobs / scheduled_job_runs / notifications 表。

说明：调度任务模块在服务启动时也会经 ensure_tables() 自建表（幂等），
本脚本为仓库约定的独立迁移入口，可在任意时刻重复执行。
用法（在 backend 目录下）：
    python migrate_scheduled_jobs_v1.py
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
    for t in ("scheduled_jobs", "scheduled_job_runs", "notifications"):
        if insp.has_table(t):
            cols = [c["name"] for c in insp.get_columns(t)]
            print(f"[migrate_scheduled_jobs_v1] {t} OK，列: {cols}")
        else:
            raise SystemExit(f"[migrate_scheduled_jobs_v1] {t} 创建失败")


if __name__ == "__main__":
    main()
