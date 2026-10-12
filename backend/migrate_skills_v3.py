"""一次性迁移脚本：skills 表新增内置技能标志列（可安全重复执行）。

变更内容：
1. skills 表新增 builtin_dialog / builtin_task 两列（默认 0）
2. 存量技能默认都是可选技能（0），首次服务启动时 sync_from_filesystem
   会按 skills_dialog/ 与 skills_task/ 目录自动回填标志

用法（在 backend 目录下）：
    python migrate_skills_v3.py
"""
import os
import sqlite3


DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.db")


def migrate():
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(skills)")
        cols = {row[1] for row in cur.fetchall()}
        for col in ("builtin_dialog", "builtin_task"):
            if col in cols:
                print(f"[INFO] skills.{col} 已存在，跳过")
                continue
            cur.execute(f"ALTER TABLE skills ADD COLUMN {col} BOOLEAN DEFAULT 0")
            print(f"[OK] skills 表新增列 {col}")
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    migrate()
