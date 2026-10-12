"""L3 目录监听：watchdog 观察 vault_dir，md 变更去抖后重索引。watchdog 未安装时降级（不监听，仅手动 reindex）。"""
import logging

logger = logging.getLogger(__name__)

import os, threading, time
from typing import Dict
from sqlalchemy.orm import Session
from app.models.knowledge import KnowledgeBase

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    _HAS_WATCHDOG = True
except Exception:
    _HAS_WATCHDOG = False
    Observer = None  # type: ignore

    class FileSystemEventHandler:  # no-op 兜底基类，保证无 watchdog 时模块仍可导入
        pass


class _Handler(FileSystemEventHandler):
    def __init__(self, watcher, kb_id):
        self.watcher = watcher
        self.kb_id = kb_id
        # 按路径独立去抖：整 KB 共用单个 timer 时，重命名产生的"旧路径删除 +
        # 新路径创建"两个事件会互相取消，旧路径的删除永远不会被处理，
        # 留下悬空文档（前端点开报"读取失败"）
        self._timers: Dict[str, threading.Timer] = {}
        self._timers_lock = threading.Lock()

    def _debounce(self, path):
        with self._timers_lock:
            old = self._timers.get(path)
            if old is not None:
                old.cancel()
            t = threading.Timer(1.5, self._fire, args=(path,))
            t.daemon = True
            self._timers[path] = t
            t.start()

    def _fire(self, path):
        with self._timers_lock:
            self._timers.pop(path, None)
        self.watcher._on_change(self.kb_id, path)

    def on_modified(self, e):
        if not getattr(e, 'is_directory', True) and e.src_path.lower().endswith(('.md', '.raw.txt')):
            self._debounce(e.src_path)

    def on_created(self, e):
        if not getattr(e, 'is_directory', True) and e.src_path.lower().endswith(('.md', '.raw.txt')):
            self._debounce(e.src_path)

    def on_deleted(self, e):
        if not getattr(e, 'is_directory', True) and e.src_path.lower().endswith(('.md', '.raw.txt')):
            self._debounce(e.src_path)

    def on_moved(self, e):
        # 重命名 = 旧路径删除 + 新路径创建，两边都要走索引流程
        if getattr(e, 'is_directory', True):
            return
        if e.src_path.lower().endswith('.md'):
            self._debounce(e.src_path)
        if e.dest_path.lower().endswith('.md'):
            self._debounce(e.dest_path)


class KnowledgeWatcher:
    """进程级单例：管理各 vault KB 的目录监听。"""
    _inst = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._inst is None:
            with cls._lock:
                if cls._inst is None:
                    cls._inst = super().__new__(cls)
                    cls._inst._observers = {}
        return cls._inst

    def available(self) -> bool:
        return _HAS_WATCHDOG

    def start_watch(self, kb_id: int, vault_dir: str):
        if not _HAS_WATCHDOG or not vault_dir or not os.path.isdir(vault_dir):
            return False
        self.stop_watch(kb_id)
        obs = Observer()
        obs.daemon = True
        obs.schedule(_Handler(self, kb_id), vault_dir, recursive=True)
        obs.start()
        self._observers[kb_id] = obs
        return True

    def stop_watch(self, kb_id: int):
        obs = self._observers.pop(kb_id, None)
        if obs:
            try:
                obs.stop(); obs.join(timeout=2)
            except Exception:
                pass

    def stop_all(self):
        for kb_id in list(self._observers.keys()):
            self.stop_watch(kb_id)

    def _on_change(self, kb_id: int, path: str):
        """去抖回调：独立 DB 会话重索引该文件。"""
        from app.database import SessionLocal
        from app.services.knowledge.markdown_indexer import MarkdownIndexer
        from app.services.knowledge.index_jobs import enqueue
        time.sleep(0.2)
        if path.lower().endswith('.raw.txt'):
            path = path[:-8] + '.md'
        db: Session = SessionLocal()
        try:
            kb = db.query(KnowledgeBase).filter(KnowledgeBase.id == kb_id).first()
            if not kb or not kb.vault_dir:
                return
            idx = MarkdownIndexer(db)
            if os.path.exists(path):
                doc_id = idx.index_file(kb, path)
                if doc_id:
                    enqueue(db, kb_id, doc_id)
            else:
                idx.remove_file(kb, path)
            db.commit()
        except Exception as e:
            logger.error(f"[watcher] 重索引失败 {path}: {e}")
        finally:
            db.close()

    def reconcile(self, kb_id: int, vault_dir: str) -> int:
        """对账：删除 vault_path 已不在磁盘上的悬空文档（连同其链接与向量）。

        修复重命名/删除事件丢失留下的脏记录（见 _Handler 去抖注释）。
        返回清理的文档数；失败只记日志不抛出。
        """
        from app.database import SessionLocal
        from app.models.knowledge import KnowledgeDocument
        from app.services.knowledge.markdown_indexer import MarkdownIndexer
        if not vault_dir or not os.path.isdir(vault_dir):
            return 0
        db: Session = SessionLocal()
        removed = 0
        try:
            kb = db.query(KnowledgeBase).filter(KnowledgeBase.id == kb_id).first()
            if not kb or not kb.vault_dir:
                return 0
            idx = MarkdownIndexer(db)
            docs = db.query(KnowledgeDocument).filter(
                KnowledgeDocument.kb_id == kb_id,
                KnowledgeDocument.vault_path.isnot(None),
            ).all()
            for d in docs:
                if not os.path.exists(os.path.join(vault_dir, d.vault_path)):
                    idx.remove_file(kb, os.path.join(vault_dir, d.vault_path))
                    removed += 1
            if removed:
                logger.info(f"[watcher] 对账清理 {removed} 个悬空文档 kb={kb_id}")
        except Exception as e:
            logger.error(f"[watcher] 对账失败 kb={kb_id}: {e}")
        finally:
            db.close()
        return removed
