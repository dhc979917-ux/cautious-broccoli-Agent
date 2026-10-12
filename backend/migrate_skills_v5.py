"""一次性迁移脚本：技能包支持（可安全重复执行）。

变更内容：
1. 新建 skill_packages 表（技能包：skills/ 下含 README.md 的目录）
2. skills 表新增 package_id 列（所属技能包，NULL=独立技能）
3. 包记录与 package_id 由服务启动时的 sync_from_filesystem 自动维护

用法（在 backend 目录下）：
    python migrate_skills_v5.py
"""
import os
import sqlite3


DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.db")


def migrate():
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS skill_packages (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                name VARCHAR(100) NOT NULL UNIQUE,
                display_name VARCHAR(200) NOT NULL,
                description VARCHAR(500),
                is_active BOOLEAN DEFAULT 1,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        print("[OK] skill_packages 表已就绪")
        cur.execute("PRAGMA table_info(skills)")
        cols = {row[1] for row in cur.fetchall()}
        if "package_id" in cols:
            print("[INFO] skills.package_id 已存在，跳过")
        else:
            cur.execute("ALTER TABLE skills ADD COLUMN package_id INTEGER")
            print("[OK] skills 表新增列 package_id")
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    migrate()
