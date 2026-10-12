"""一次性迁移脚本：给 knowledge_bases 加 scope 列（personal/shared），存量库置 shared。

背景：原知识库均为管理员创建、全员可见，等价于 shared；个人库（personal）是新概念，
仅属主 + 管理员可见/管理。Agent 检索时按当前用户并入其个人库（方案B）。

用法（在 backend 目录下）：
    python migrate_knowledge_scope_v1.py
"""
import os
import sqlite3

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.db")
MARKER_KEY = "knowledge_scope_v1"


def migrate():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    try:
        cur = conn.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS _migrations (key TEXT PRIMARY KEY, done_at TEXT)")
        cur.execute("SELECT 1 FROM _migrations WHERE key = ?", (MARKER_KEY,))
        if cur.fetchone():
            print("[INFO] 迁移已执行过，跳过")
            return
        # 幂等加列：sqlite 不支持 ADD COLUMN IF NOT EXISTS，先自查
        cur.execute("PRAGMA table_info(knowledge_bases)")
        cols = [r[1] for r in cur.fetchall()]
        if "scope" not in cols:
            cur.execute(
                "ALTER TABLE knowledge_bases ADD COLUMN scope VARCHAR(20) DEFAULT 'shared' NOT NULL"
            )
            print("[OK] 已添加 knowledge_bases.scope 列")
        else:
            print("[INFO] scope 列已存在，跳过加列")
        cur.execute(
            "UPDATE knowledge_bases SET scope = COALESCE(scope, 'shared') "
            "WHERE scope IS NULL OR scope = ''"
        )
        print(f"[OK] 存量库置 shared {cur.rowcount} 条")
        cur.execute("INSERT INTO _migrations (key, done_at) VALUES (?, datetime('now'))", (MARKER_KEY,))
        conn.commit()
        print("[DONE] 迁移完成")
    finally:
        conn.close()


if __name__ == "__main__":
    migrate()
