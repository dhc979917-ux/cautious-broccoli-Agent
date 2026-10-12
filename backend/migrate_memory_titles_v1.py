"""一次性迁移脚本：个人记忆库总结层文档标题统一（day-/week-/month-）。

背景：存量 digest 文档的标题取自 LLM 生成的 H1，无统一规范；本迁移把统一标题
写入各 digest 文件的 frontmatter（title: 字段）并重索引，使文档列表与图谱的
显示名一致（新文件由 janitor 写入时直接带 title 元数据，无需再迁移）。

命名规则：
    日总结  digest/2026-08-26.md        -> day-2026-08-26
    周总结  digest/weekly-2026-W35.md   -> week-2026-08-4（年-月-当月第几周，按周一归月）
    月总结  digest/monthly-2026-08.md   -> month-2026-08

用法（在 backend 目录下）：
    python migrate_memory_titles_v1.py
"""
import os
import re
import sqlite3
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.db")
MARKER_KEY = "memory_titles_v1"
MEMORY_KB_PREFIX = "\u6211\u7684\u8bb0\u5fc6-"  # "我的记忆-"

_DIGEST_DAILY = re.compile(r"^(\d{4}-\d{2}-\d{2})\.md$")
_DIGEST_WEEK = re.compile(r"^weekly-(\d{4})-W(\d{2})\.md$")
_DIGEST_MONTH = re.compile(r"^monthly-(\d{4}-\d{2})\.md$")


def _week_display_name(iso_year: str, iso_week: str) -> str:
    """ISO 周 -> week-年-月-当月第几周（按该周周一归月）。"""
    try:
        monday = datetime.strptime(f"{iso_year}-W{iso_week}-1", "%G-W%V-%u")
    except ValueError:
        return f"week-{iso_year}-W{iso_week}"
    return f"week-{monday.year:04d}-{monday.month:02d}-{(monday.day - 1) // 7 + 1}"


def _unified_title(fn: str):
    m = _DIGEST_DAILY.match(fn)
    if m:
        return f"day-{m.group(1)}"
    m = _DIGEST_WEEK.match(fn)
    if m:
        return _week_display_name(m.group(1), m.group(2))
    m = _DIGEST_MONTH.match(fn)
    if m:
        return f"month-{m.group(1)}"
    return None


def _set_frontmatter_title(path: str, title: str) -> bool:
    """把 frontmatter 的 title 设为统一标题；已一致则不动。返回是否有写入。"""
    with open(path, encoding="utf-8") as f:
        text = f.read()
    if re.search(rf"^title:\s*{re.escape(title)}\s*$", text, re.M):
        return False
    if re.search(r"^title:", text, re.M):
        text = re.sub(r"^title:.*$", f"title: {title}", text, count=1, flags=re.M)
    elif text.startswith("---"):
        text = text.replace("---\n", f"---\ntitle: {title}\n", 1)
    else:
        text = f"---\ntitle: {title}\n---\n\n{text}"
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return True


def migrate():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        cur = conn.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS _migrations (key TEXT PRIMARY KEY, done_at TEXT)")
        cur.execute("SELECT 1 FROM _migrations WHERE key = ?", (MARKER_KEY,))
        if cur.fetchone():
            print("[INFO] \u8fc1\u79fb\u5df2\u6267\u884c\u8fc7\uff0c\u8df3\u8fc7")
            return
    finally:
        conn.close()

    from app.database import SessionLocal
    from app.models.knowledge import KnowledgeBase
    from app.services.knowledge.markdown_indexer import MarkdownIndexer

    patched = 0
    db = SessionLocal()
    try:
        idx = MarkdownIndexer(db)
        kbs = (
            db.query(KnowledgeBase)
            .filter(
                KnowledgeBase.scope == "personal",
                KnowledgeBase.name.like(MEMORY_KB_PREFIX + "%"),
            )
            .all()
        )
        for kb in kbs:
            if not kb.vault_dir:
                continue
            digest_dir = os.path.join(kb.vault_dir, "digest")
            if not os.path.isdir(digest_dir):
                continue
            for fn in sorted(os.listdir(digest_dir)):
                title = _unified_title(fn)
                if not title:
                    continue
                path = os.path.join(digest_dir, fn)
                if _set_frontmatter_title(path, title):
                    idx.index_file(kb, path)
                    patched += 1
                    print(f"[OK] kb={kb.id} {fn} -> {title}")
        db.commit()
    finally:
        db.close()

    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        conn.execute(
            "INSERT INTO _migrations (key, done_at) VALUES (?, datetime('now'))",
            (MARKER_KEY,),
        )
        conn.commit()
    finally:
        conn.close()
    print(f"[DONE] \u8fc1\u79fb\u5b8c\u6210\uff0c\u5171\u66f4\u65b0 {patched} \u4e2a\u6587\u4ef6\u6807\u9898")


if __name__ == "__main__":
    migrate()
