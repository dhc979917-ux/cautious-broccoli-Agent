"""一次性迁移脚本：创建 session_context 表（会话上下文持久化）+ app_schedules 补 attendees 列。
说明：init_db() 启动时也会经 Base.metadata.create_all 建表、_migrate_add_columns 补列，
本脚本为仓库约定的独立迁移入口，幂等，可在任意时刻执行（本地 SQLite / 服务器 PostgreSQL 通用）。
用法（在 backend 目录下）：
    python migrate_session_context_v1.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.database import Base, engine  # noqa: E402
from app.models import session_context, schedule  # noqa: E402  (登记模型到 Base)


def main():
    Base.metadata.create_all(bind=engine)
    from sqlalchemy import inspect, text
    insp = inspect(engine)

    if insp.has_table("session_context"):
        cols = [c["name"] for c in insp.get_columns("session_context")]
        print(f"[migrate_session_context_v1] session_context OK，列：{cols}")
    else:
        raise SystemExit("[migrate_session_context_v1] session_context 创建失败")

    # 存量 app_schedules 补 attendees 列（新建库经 create_all 已带该列）
    if insp.has_table("app_schedules"):
        cols = {c["name"] for c in insp.get_columns("app_schedules")}
        if "attendees" not in cols:
            with engine.connect() as conn:
                conn.execute(text("ALTER TABLE app_schedules ADD COLUMN attendees TEXT"))
                conn.commit()
            print("[migrate_session_context_v1] app_schedules.attendees 列已补齐")
        else:
            print("[migrate_session_context_v1] app_schedules.attendees 列已存在")


if __name__ == "__main__":
    main()
