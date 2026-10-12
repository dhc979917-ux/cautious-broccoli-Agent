"""一次性迁移脚本：skills 表新增中文展示列（可安全重复执行）。

变更内容：
1. skills 表新增 display_name_zh / description_zh 两列（可空）
2. 列值由服务启动时的 translate_pending_skills 自动生成（qwen3.7-flash）

用法（在 backend 目录下）：
    python migrate_skills_v4.py
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
        for col, ddl in (
            ("display_name_zh", "ALTER TABLE skills ADD COLUMN display_name_zh VARCHAR(200)"),
            ("description_zh", "ALTER TABLE skills ADD COLUMN description_zh VARCHAR(500)"),
        ):
            if col in cols:
                print(f"[INFO] skills.{col} 已存在，跳过")
                continue
            cur.execute(ddl)
            print(f"[OK] skills 表新增列 {col}")
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    migrate()
