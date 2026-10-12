"""一次性迁移脚本：技能广场可见性 + 用户技能授权（幂等，可安全重复执行）。

变更内容：
1. skills / skill_packages 新增 is_hidden（管理员态，不由文件系统同步维护）
2. 新建 user_skill_access 表（user_id + target_type('skill'|'package') + target_id）
   - 包授权自动覆盖包内后续新增成员技能
   - 授权覆盖 required_role 与广场隐藏：被授权用户始终可见且可用

说明：服务启动时 init_db() 也会幂等补齐这些结构（create_all + _migrate_add_columns），
本脚本是仓库约定的独立迁移入口，负责校验与部署后即时对账。
用法（在 backend 目录下）：
    python migrate_skill_access_v1.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def ensure_schema():
    from app.database import init_db
    init_db()
    print("[OK] 表结构已就绪（init_db 幂等补齐）")


def verify():
    from sqlalchemy import inspect
    from app.database import engine
    insp = inspect(engine)
    for table, col in (("skills", "is_hidden"), ("skill_packages", "is_hidden")):
        if col not in {c["name"] for c in insp.get_columns(table)}:
            raise SystemExit(f"[migrate_skill_access_v1] 缺列: {table}.{col}")
    if not insp.has_table("user_skill_access"):
        raise SystemExit("[migrate_skill_access_v1] 缺表: user_skill_access")
    print("[OK] migrate_skill_access_v1 校验通过：is_hidden 列齐备、user_skill_access 已就绪")


def main():
    ensure_schema()
    verify()


if __name__ == "__main__":
    main()
