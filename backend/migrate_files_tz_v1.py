"""一次性迁移脚本：统一存量时间戳为北京时间 + 清理文件来源孤儿引用（可安全重复执行）。

背景：
1. files/sessions/messages 旧数据由 SQLite CURRENT_TIMESTAMP（UTC）写入，
   前端按本地时间展示导致差 8 小时（如北京时间 14:35 显示成 06:35）。
   新代码已改为 Python datetime.now()（北京时间）写入，这里把存量 +8h 修正。
   files.created_at 里"对账补录"的记录本来就是本地时间（带微秒），不动。
2. 会话删除后 files.session_id 残留指向已删会话（来源死链），置空清理。

用法（在 backend 目录下）：
    python migrate_files_tz_v1.py
"""
import os
import sqlite3


DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.db")
MARKER_KEY = "tz_beijing_v1"


def migrate():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    try:
        cur = conn.cursor()
        # 幂等守卫：跑过一次就跳过
        cur.execute("CREATE TABLE IF NOT EXISTS _migrations (key TEXT PRIMARY KEY, done_at TEXT)")
        cur.execute("SELECT 1 FROM _migrations WHERE key = ?", (MARKER_KEY,))
        if cur.fetchone():
            print("[INFO] 迁移已执行过，跳过")
            return

        # --- 1) 时间修正（仅处理 UTC 写入的记录）---
        # files.created_at：秒级精度（无小数点）= server_default UTC 写入 → +8h；
        #                    带微秒的 = 对账脚本用本地时间写入 → 不动
        cur.execute("""
            UPDATE files SET created_at = datetime(created_at, '+8 hours')
            WHERE created_at IS NOT NULL AND instr(created_at, '.') = 0
        """)
        print(f"[OK] files.created_at 修正 {cur.rowcount} 条")

        # files.deleted_at：旧代码用 datetime.now(timezone.utc) 写入，存储带 +00:00 后缀
        cur.execute("""
            UPDATE files SET deleted_at = datetime(substr(deleted_at, 1, 19), '+8 hours')
            WHERE deleted_at LIKE '%+00:00%'
        """)
        print(f"[OK] files.deleted_at 修正 {cur.rowcount} 条")

        # sessions / messages：全部来自 server_default UTC → 整体 +8h（排序不受影响）
        cur.execute("UPDATE sessions SET created_at = datetime(created_at, '+8 hours') WHERE created_at IS NOT NULL")
        print(f"[OK] sessions.created_at 修正 {cur.rowcount} 条")
        cur.execute("UPDATE sessions SET updated_at = datetime(updated_at, '+8 hours') WHERE updated_at IS NOT NULL")
        print(f"[OK] sessions.updated_at 修正 {cur.rowcount} 条")
        cur.execute("UPDATE messages SET created_at = datetime(created_at, '+8 hours') WHERE created_at IS NOT NULL")
        print(f"[OK] messages.created_at 修正 {cur.rowcount} 条")

        # --- 2) 清理孤儿来源引用（会话已删，文件还指着它）---
        cur.execute("""
            UPDATE files SET session_id = NULL
            WHERE session_id IS NOT NULL
              AND session_id NOT IN (SELECT id FROM sessions)
        """)
        print(f"[OK] 清理孤儿来源引用 {cur.rowcount} 条")

        cur.execute("INSERT INTO _migrations (key, done_at) VALUES (?, datetime('now'))", (MARKER_KEY,))
        conn.commit()
        print("[DONE] 迁移完成")
    finally:
        conn.close()


if __name__ == "__main__":
    migrate()
