"""一次性迁移脚本：skill_packages 表新增包级权限列（可安全重复执行）。

变更内容：
1. skill_packages 新增 required_role 列（可空）
2. 包设置权限时，成员技能统一沿用（由 update_package 维护）；
   不设置（NULL）时成员技能保留各自权限

用法（在 backend 目录下）：
    python migrate_skills_v6.py
"""
import os
import sqlite3


DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.db")


def migrate():
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(skill_packages)")
        cols = {row[1] for row in cur.fetchall()}
        if "required_role" in cols:
            print("[INFO] skill_packages.required_role 已存在，跳过")
        else:
            cur.execute("ALTER TABLE skill_packages ADD COLUMN required_role VARCHAR(20)")
            print("[OK] skill_packages 表新增列 required_role")
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    migrate()
