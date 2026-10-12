"""一次性迁移脚本：创建 service_previews 表（服务预览状态机）。

服务启动时 init_db() 的 create_all 也会建这张表（幂等），本脚本是仓库约定的
独立迁移入口，可在任意时刻重复执行；SQLite 与 Postgres 两种方言均可运行。

用法（在 backend 目录下）：
    python migrate_service_previews_v1.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 本表需要的全部列：建表后逐列校验，缺列直接失败（避免半迁移状态上线）
_REQUIRED_COLUMNS = (
    "id", "user_id", "session_id", "agent_id", "container_name", "workspace_id",
    "target_host", "target_port", "bind_loopback", "proxy_container", "host_port",
    "project_dir", "entry_command", "process_name", "bundle_path", "bundle_size",
    "status", "detail", "created_at", "last_seen_at", "expires_at",
)


def main():
    from sqlalchemy import inspect
    from app.database import Base, engine
    from app.models.service_preview import ServicePreview

    # create_all 只建缺失的表，已存在则原样跳过 -> 天然幂等
    ServicePreview.__table__.create(bind=engine, checkfirst=True)

    insp = inspect(engine)
    if "service_previews" not in insp.get_table_names():
        raise SystemExit("[migrate_service_previews_v1] 建表失败: service_previews 不存在")
    cols = [c["name"] for c in insp.get_columns("service_previews")]
    missing = [c for c in _REQUIRED_COLUMNS if c not in cols]
    if missing:
        raise SystemExit(f"[migrate_service_previews_v1] 缺列: {missing}")
    print(f"[migrate_service_previews_v1] service_previews OK，共 {len(cols)} 列")


if __name__ == "__main__":
    main()
