"""迁移：Obsidian 模块。加 knowledge_bases.vault_dir、knowledge_documents 笔记元数据列、knowledge_links 表。幂等。"""
import os, sqlite3
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.db")
MARKER = "knowledge_obsidian_v1"

def _add_col(cur, table, col, ddl):
    cur.execute("PRAGMA table_info(%s)" % table)
    cols = [r[1] for r in cur.fetchall()]
    if col not in cols:
        cur.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, col, ddl))

def migrate():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    try:
        cur = conn.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS _migrations (key TEXT PRIMARY KEY, done_at TEXT)")
        cur.execute("SELECT 1 FROM _migrations WHERE key = ?", (MARKER,))
        if cur.fetchone():
            print("[INFO] 迁移已执行过，跳过"); return
        _add_col(cur, "knowledge_bases", "vault_dir", "TEXT")
        for col, ddl in [
            ("title", "VARCHAR(255)"), ("vault_path", "TEXT"), ("frontmatter_json", "TEXT"),
            ("tags_json", "TEXT"), ("category", "VARCHAR(100)"), ("content_hash", "VARCHAR(64)"),
            ("is_markdown", "BOOLEAN DEFAULT 0"),
        ]:
            _add_col(cur, "knowledge_documents", col, ddl)
        cur.execute("""CREATE TABLE IF NOT EXISTS knowledge_links (
            id INTEGER PRIMARY KEY, kb_id INTEGER NOT NULL,
            src_id INTEGER NOT NULL, dst_id INTEGER, dst_name TEXT NOT NULL,
            kind VARCHAR(20) NOT NULL, anchor TEXT, context_snippet TEXT)""")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_kl_kb ON knowledge_links(kb_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_kl_src ON knowledge_links(src_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_kl_dst ON knowledge_links(dst_id)")
        cur.execute("INSERT INTO _migrations (key, done_at) VALUES (?, datetime('now'))", (MARKER,))
        conn.commit()
        print("[DONE] Obsidian 迁移完成")
    finally:
        conn.close()

if __name__ == "__main__":
    migrate()
