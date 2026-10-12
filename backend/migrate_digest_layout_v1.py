"""一次性迁移脚本：个人记忆库总结卡改存 digest/day|week|month 三层目录。

背景：日/周/月总结此前混放在 digest/ 根下，靠文件名前缀区分（weekly- / monthly-），
扫描与图谱归类都要写一堆前缀过滤。新布局按层级分目录::

    digest/2026-09-04.md        -> digest/day/2026-09-04.md
    digest/weekly-2026-W35.md   -> digest/week/weekly-2026-W35.md
    digest/monthly-2026-08.md   -> digest/month/monthly-2026-08.md

路径定义只在 app/agents/memory_layout.py 一处，本脚本不重复硬编码。

动作（每个个人记忆库）：
1) 停掉本进程的 vault watcher，避免移动被看成"旧路径删除 + 新路径新建"
   （那样会换 doc id、连带删掉 画像->总结 的 auto 边并重新 embedding）
2) 按 memory_layout.migration_plan 移动文件
3) DB 原地更新 knowledge_documents.vault_path —— 保住 doc id，向量（按
   document_id=doc.id 存）与链接都不用重建，不需要重新 embedding
4) 把周/月卡 frontmatter 的 absorbed 改写成新的 vault 相对路径（图谱溯源用）
5) 重索引被移动的文档，刷新 wikilink 目标解析（name map 以 vault_path 为键）
6) 幂等补一次 画像->总结卡 的 auto 边：万一有别的进程（运行中的后端 watcher）
   抢先把旧行删了，这一步把边重新接上
7) 恢复 watcher

ReME 检索索引不需要在这里动：后端下次启动时 init_changes_step 会做全量对账
（to_delete = 已索引 - 磁盘现存），旧路径自动淘汰、新路径自动纳入；后端在运行时
watch_changes_step 也会即时跟进。若想在脚本里就把它落盘，加 --reindex（要求后端
已停止，否则两个进程同时写 ReME 的 metadata/ 索引有损坏风险）。

幂等：已迁移的库 migration_plan 为空，重复执行不做任何事。
SQLite 与 Postgres 两种方言均可运行。

用法（在 backend 目录下，建议先停后端）：
    python migrate_digest_layout_v1.py            # 实际执行
    python migrate_digest_layout_v1.py --dry-run  # 只打印计划
    python migrate_digest_layout_v1.py --reindex  # 迁移后显式同步 ReME 索引
"""
import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

MEMORY_KB_PREFIX = "\u6211\u7684\u8bb0\u5fc6-"  # "我的记忆-"


def _rewrite_absorbed(abs_path: str, moved: dict) -> bool:
    """把周/月卡 frontmatter 里 absorbed 的旧路径/裸文件名改写成新相对路径。

    moved: {旧 rel 或裸文件名: 新 rel}。没有可改的返回 False（不写盘）。
    """
    from app.agents import memory_layout as mlayout
    try:
        with open(abs_path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return False
    if not text.startswith("---"):
        return False  # 没有 frontmatter，absorbed 不在正文里找
    end = text.find("\n---", 3)
    if end < 0:
        return False
    lines = text.splitlines(keepends=True)
    changed = False
    fm_lines = text[:end].count("\n") + 1  # 只在 frontmatter 范围内改写
    for i, line in enumerate(lines):
        if i > fm_lines:
            break
        if not line.startswith("absorbed:"):
            continue
        entries = [e.strip() for e in line.split(":", 1)[1].strip().strip("'\"").split(",") if e.strip()]
        new_entries = []
        for e in entries:
            hit = mlayout.classify(e) or mlayout.classify_filename(e)
            new_entries.append(mlayout.rel_path(hit[0], hit[1]) if hit else moved.get(e, e))
        if new_entries and new_entries != entries:
            lines[i] = f"absorbed: {','.join(new_entries)}\n"
            changed = True
        break
    if changed:
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write("".join(lines))
    return changed


def _migrate_kb(db, kb, dry_run: bool) -> dict:
    from app.agents import memory_layout as mlayout
    from app.models.knowledge import KnowledgeDocument
    from app.services.knowledge.markdown_indexer import MarkdownIndexer
    from app.services.knowledge.watcher import KnowledgeWatcher

    out = {"kb": kb.id, "vault": kb.vault_dir, "moved": 0, "db_updated": 0,
           "db_missing": 0, "db_conflict": 0, "absorbed": 0, "relinked": 0, "errors": []}
    vault = kb.vault_dir
    if not vault or not os.path.isdir(vault):
        out["errors"].append("vault 目录不存在")
        return out
    plan = mlayout.migration_plan(vault)
    if not plan:
        return out
    if dry_run:
        out["moved"] = len(plan)
        out["plan"] = plan
        return out

    idx = MarkdownIndexer(db)
    watcher = KnowledgeWatcher()
    # 本进程的 watcher 先停：移动若被看成"删除+新建"，旧 doc 行会被删掉，
    # 连带 画像->总结 的 auto 边和向量一起没，还要重新 embedding。
    # （后端在另一个进程里跑时停不到它的 watcher，靠后面的 auto 边补写兜底）
    try:
        watcher.stop_watch(kb.id)
    except Exception:
        pass
    moved_map = {}
    try:
        for src_rel, dst_rel in plan:
            src_abs = os.path.join(vault, *src_rel.split("/"))
            dst_abs = os.path.join(vault, *dst_rel.split("/"))
            try:
                os.makedirs(os.path.dirname(dst_abs), exist_ok=True)
                os.replace(src_abs, dst_abs)
            except OSError as e:
                out["errors"].append(f"移动失败 {src_rel}: {e}")
                continue
            out["moved"] += 1
            moved_map[src_rel] = dst_rel
            moved_map[os.path.basename(src_rel)] = dst_rel
            # DB 原地改路径，保住 doc id（向量按 doc id 存，链接挂在 doc id 上）
            src_row = db.query(KnowledgeDocument).filter(
                KnowledgeDocument.kb_id == kb.id,
                KnowledgeDocument.vault_path == src_rel,
            ).first()
            dst_row = db.query(KnowledgeDocument).filter(
                KnowledgeDocument.kb_id == kb.id,
                KnowledgeDocument.vault_path == dst_rel,
            ).first()
            if src_row is None:
                out["db_missing"] += 1
            elif dst_row is not None:
                # 目标行已被别的进程建出来了：留新行，清掉旧行（含链接与向量）
                out["db_conflict"] += 1
                idx.remove_file(kb, src_abs)
            else:
                src_row.vault_path = dst_rel
                db.flush()
                out["db_updated"] += 1
            # 刷新该文档的链接解析（name map 以 vault_path 为键）
            try:
                idx.index_file(kb, dst_abs)
            except Exception as e:
                out["errors"].append(f"重索引失败 {dst_rel}: {e}")
        db.commit()

        # absorbed 改写：只处理周/月卡（日卡没有 absorbed 字段）
        for level in (mlayout.WEEK, mlayout.MONTH):
            for card in mlayout.iter_cards(vault, level):
                try:
                    if _rewrite_absorbed(card.abs, moved_map):
                        out["absorbed"] += 1
                        idx.index_file(kb, card.abs)
                except Exception as e:
                    out["errors"].append(f"absorbed 改写失败 {card.rel}: {e}")
        db.commit()

        # 幂等补 画像->总结卡 的 auto 边（若旧行被别的进程删过，边会一起没）
        out["relinked"] = _relink_profile_edges(vault)
    finally:
        # start_watch 内部会先 stop 再 schedule；watchdog 没装时返回 False，无副作用
        try:
            watcher.start_watch(kb.id, vault)
        except Exception as e:
            out["errors"].append(f"恢复 watcher 失败: {e}")
    return out


def _relink_profile_edges(vault: str) -> int:
    """对 vault 里每张总结卡补一次 画像->总结卡 的 auto 边，返回处理的卡数。

    复用 janitor 的 _link_profile_to_card（内部幂等：边已存在就不重复插），
    这样迁移与日常运行走的是同一套落边逻辑，不会出现两套实现漂移。
    """
    from app.agents import memory_layout as mlayout
    try:
        from app.agents.memory import _link_profile_to_card
    except Exception as e:  # agentscope 不可用时跳过，不影响文件与 DB 迁移
        print(f"[migrate_digest_layout_v1] 跳过 auto 边补写（导入 memory 失败）: {e}")
        return 0
    labels = {mlayout.DAY: "当日总结", mlayout.WEEK: "周总结", mlayout.MONTH: "月度"}
    n = 0
    for level in mlayout.LEVELS:
        for card in mlayout.iter_cards(vault, level):
            try:
                _link_profile_to_card(vault, card.rel, f"{labels[level]}-{card.key}")
                n += 1
            except Exception as e:
                print(f"[migrate_digest_layout_v1] auto 边补写失败 {card.rel}: {e}")
    return n


async def _reindex_users(user_ids: list) -> None:
    """显式把新路径同步进 ReME 检索索引并落盘（要求后端已停止）。"""
    from app.agents.memory import get_memory_middleware, sync_memory_index
    from app.agents import memory_layout as mlayout
    for uid in user_ids:
        mw = get_memory_middleware(uid)
        if mw is None:
            print(f"[migrate_digest_layout_v1] user={uid} 无记忆中间件，跳过索引同步")
            continue
        try:
            await mw._ensure_started()  # noqa: SLF001
            vault = os.path.abspath(mw._workspace_dir)  # noqa: SLF001
            paths = []
            for level in mlayout.LEVELS:
                paths += [c.abs for c in mlayout.iter_cards(vault, level)]
            ok = await sync_memory_index(mw._app, vault, paths, who=f"migrate user={uid}")  # noqa: SLF001
            print(f"[migrate_digest_layout_v1] user={uid} ReME 索引同步 {'成功' if ok else '失败'}（{len(paths)} 张卡）")
        except Exception as e:
            print(f"[migrate_digest_layout_v1] user={uid} ReME 索引同步异常: {e}")
        finally:
            try:
                await mw.close()
            except Exception:
                pass


def main():
    ap = argparse.ArgumentParser(description="个人记忆库总结卡迁移到 digest/day|week|month")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不动文件与数据库")
    ap.add_argument("--reindex", action="store_true",
                    help="迁移后显式同步 ReME 检索索引（要求后端已停止）")
    args = ap.parse_args()

    from app.database import SessionLocal
    from app.models.knowledge import KnowledgeBase

    db = SessionLocal()
    results = []
    try:
        kbs = db.query(KnowledgeBase).filter(
            KnowledgeBase.scope == "personal",
            KnowledgeBase.name.like(MEMORY_KB_PREFIX + "%"),
            KnowledgeBase.vault_dir.isnot(None),
        ).all()
        print(f"[migrate_digest_layout_v1] 命中个人记忆库 {len(kbs)} 个")
        for kb in kbs:
            r = _migrate_kb(db, kb, args.dry_run)
            results.append(r)
            if args.dry_run:
                for src, dst in r.get("plan", []):
                    print(f"  [dry-run] kb={kb.id} {src} -> {dst}")
            else:
                print(f"  kb={kb.id} 移动 {r['moved']} 张，DB 更新 {r['db_updated']}，"
                      f"缺行 {r['db_missing']}，冲突 {r['db_conflict']}，"
                      f"absorbed 改写 {r['absorbed']}，auto 边补写 {r['relinked']}")
                for e in r["errors"]:
                    print(f"    [warn] {e}")
    finally:
        db.close()

    total = sum(r["moved"] for r in results)
    print(f"[migrate_digest_layout_v1] 完成：{'计划' if args.dry_run else '迁移'} {total} 张总结卡")
    if args.reindex and not args.dry_run:
        uids = []
        db = SessionLocal()
        try:
            for kb in db.query(KnowledgeBase).filter(
                KnowledgeBase.scope == "personal",
                KnowledgeBase.name.like(MEMORY_KB_PREFIX + "%"),
            ).all():
                if kb.created_by is not None:
                    uids.append(kb.created_by)
        finally:
            db.close()
        asyncio.run(_reindex_users(sorted(set(uids))))


if __name__ == "__main__":
    main()
