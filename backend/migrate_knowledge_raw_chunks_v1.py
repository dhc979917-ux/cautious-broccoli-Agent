"""一次性回填脚本：把存量"源文档转换笔记"的向量改为按清洗后原文切块。

背景：旧管线对 vault 里 LLM 改写后的 md 切块入库；新管线改为对"清洗后的
源文档原文"切块入库（旁挂 <笔记名>.raw.txt），改写 md 仅作展示。
本脚本遍历 frontmatter 带 source_file 的笔记，重新提取原始上传文件、
规则清洗、写旁挂原文，然后重走向量化（ingest_vault_md 自动优先读旁挂原文）。
md 展示内容不重写（如需重改写请删除文档重新上传）。

用法（在 backend 目录下，conda env agentscope）：
    python migrate_knowledge_raw_chunks_v1.py
幂等：_migrations 表打点；重复执行直接跳过。
"""
import asyncio
import json
import os
import sqlite3
import sys

# Windows 控制台默认 GBK，打印含特殊 Unicode（如 ₂）的文件名会 UnicodeEncodeError。
# 统一把标准输出/错误重配为 UTF-8（替换不可编码字符），保证脚本不会因打印中断。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _resolve_db_path() -> str:
    """取 settings 配置的 sqlite 库路径；非 sqlite/取不到时退回 backend/app.db。"""
    try:
        from app.config import settings
        url = settings.DATABASE_URL or ""
        if url.startswith("sqlite:///"):
            p = url[len("sqlite:///"):]
            return p if os.path.isabs(p) else os.path.join(os.path.dirname(os.path.abspath(__file__)), p)
    except Exception:
        pass
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.db")


DB_PATH = _resolve_db_path()
MARKER_KEY = "knowledge_raw_chunks_v1"


def _already_done() -> bool:
    conn = sqlite3.connect(DB_PATH, timeout=15)
    try:
        cur = conn.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS _migrations (key TEXT PRIMARY KEY, done_at TEXT)")
        cur.execute("SELECT 1 FROM _migrations WHERE key = ?", (MARKER_KEY,))
        row = cur.fetchone()
        conn.commit()
        return bool(row)
    finally:
        conn.close()


def _mark_done():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    try:
        cur = conn.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS _migrations (key TEXT PRIMARY KEY, done_at TEXT)")
        cur.execute(
            "INSERT OR REPLACE INTO _migrations (key, done_at) VALUES (?, datetime('now'))",
            (MARKER_KEY,),
        )
        conn.commit()
    finally:
        conn.close()


async def backfill():
    from app.config import settings
    from app.database import SessionLocal
    from app.models.knowledge import KnowledgeBase, KnowledgeDocument
    from app.services.knowledge_service import KnowledgeService, CONVERTIBLE_EXTS
    from app.services.knowledge.source_to_md_service import SourceToMdService
    from app.services.knowledge.raw_cleaner import clean_extracted_text

    svc = SourceToMdService()
    db = SessionLocal()
    svc2 = KnowledgeService(db)
    done, skipped, failed = 0, 0, 0
    try:
        docs = (
            db.query(KnowledgeDocument)
            .filter(KnowledgeDocument.is_markdown == True)  # noqa: E712
            .all()
        )
        for doc in docs:
            try:
                meta = json.loads(doc.frontmatter_json or "{}")
            except Exception:
                meta = {}
            source_file = meta.get("source_file")
            if not source_file or meta.get("type") != "source":
                skipped += 1
                continue
            kb = db.query(KnowledgeBase).filter(KnowledgeBase.id == doc.kb_id).first()
            if not kb or not kb.vault_dir or not doc.vault_path:
                skipped += 1
                continue
            ext = os.path.splitext(source_file)[1].lower()
            if ext not in CONVERTIBLE_EXTS:
                skipped += 1
                continue
            src_path = os.path.join(settings.KNOWLEDGE_DIR, str(kb.id), source_file)
            if not os.path.isfile(src_path):
                print(f"[SKIP] doc={doc.id} 原始文件不存在: {source_file}")
                skipped += 1
                continue
            md_path = os.path.join(kb.vault_dir, doc.vault_path)
            if not os.path.isfile(md_path):
                print(f"[SKIP] doc={doc.id} vault 笔记不存在: {doc.vault_path}")
                skipped += 1
                continue
            raw_path = os.path.splitext(md_path)[0] + ".raw.txt"
            try:
                raw_md = await asyncio.to_thread(
                    svc._extract_to_markdown, src_path, source_file, ext
                )
                cleaned = clean_extracted_text(raw_md) or raw_md
                with open(raw_path, "w", encoding="utf-8") as f:
                    f.write(cleaned)
                await svc2.ingest_vault_md(doc.id)
                done += 1
                print(f"[OK] doc={doc.id} {doc.filename} -> 原文切块重入库")
            except Exception as e:
                failed += 1
                print(f"[FAIL] doc={doc.id} {doc.filename}: {e}")
    finally:
        db.close()
    print(f"[DONE] 重入库 {done} 篇，跳过 {skipped}，失败 {failed}")


def main():
    if _already_done():
        print("[INFO] 回填已执行过，跳过")
        return
    asyncio.run(backfill())
    _mark_done()


if __name__ == "__main__":
    main()
