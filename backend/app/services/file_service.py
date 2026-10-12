"""文件中心服务

职责：files 元数据表的增删查改。内容存储在本地磁盘（uploads/ 与
outputs/user_{id}/），stored_name 即相对路径，权限隔离原则：所有查询默认
强制 user_id 过滤，管理员视角由 API 层显式放开。
"""

import logging

logger = logging.getLogger(__name__)

import os
import re
from datetime import datetime
from typing import Optional, List

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import UPLOAD_DIR, OUTPUT_DIR, settings
from app.models.file import FileRecord
from app.models.user import User
from app.models.session import Session as ChatSession
from app.models.agent import Agent

# uploads 目录文件名格式：{user_id}_{timestamp}_{safe_name}
UPLOAD_NAME_RE = re.compile(r"^(\d+)_(\d+)_(.+)$")
# outputs 目录下的用户子目录：user_{id}
OUTPUT_USER_DIR_RE = re.compile(r"^user_(\d+)$")


class FileService:
    def __init__(self, db: Session):
        self.db = db

    # ---------- 入库挂载 ----------

    def register_upload(self, user_id: int, stored_name: str, original_name: str,
                        size: int, content_type: Optional[str] = None) -> FileRecord:
        """用户上传成功后登记元数据"""
        rec = FileRecord(
            user_id=user_id, kind="upload",
            original_name=original_name or stored_name,
            stored_name=stored_name, size=size, content_type=content_type,
        )
        self.db.add(rec)
        self.db.commit()
        self.db.refresh(rec)
        return rec

    def register_output(self, user_id: int, filename: str, size: int,
                        session_id: Optional[int] = None,
                        agent_id: Optional[int] = None,
                        session_title: Optional[str] = None) -> FileRecord:
        """技能执行产出文件登记（同名文件重复生成时更新原记录）。
        filename 可能是嵌套相对路径（projects/xxx/a.py），展示名取 basename。

        session_title 为来源会话标题快照：不传且给了 session_id 时自动查一次，
        会话日后被删除也能继续在文件中心展示"来源"（置灰不可点）。"""
        if session_title is None and session_id:
            sess = self.db.query(ChatSession).filter(ChatSession.id == session_id).first()
            session_title = sess.title if sess else None
        rec = self.db.query(FileRecord).filter(
            FileRecord.user_id == user_id,
            FileRecord.kind == "output",
            FileRecord.stored_name == filename,
        ).first()
        if rec:
            rec.size = size
            rec.deleted_at = None  # 重新生成则从回收站复活
            rec.created_at = datetime.now()  # 时间同步刷新为本次生成时间（此前一直停在首次生成时间）
            if session_id:
                rec.session_id = session_id
            if agent_id:
                rec.agent_id = agent_id
            if session_title:
                rec.session_title = session_title
        else:
            rec = FileRecord(
                user_id=user_id, kind="output",
                original_name=os.path.basename(filename.replace("\\", "/")),
                stored_name=filename, size=size,
                session_id=session_id, agent_id=agent_id,
                session_title=session_title,
            )
            self.db.add(rec)
        self.db.commit()
        self.db.refresh(rec)
        return rec

    def register_output_file(
        self,
        *,
        user_id: int,
        session_id: Optional[int],
        agent_id: Optional[int],
        rel_name: str,
        abs_path: str,
    ) -> FileRecord:
        """把一个平台自己生成的产出文件登记进文件中心（元数据入库）。

        与 register_output 的区别：这里负责"从磁盘上的绝对路径 -> 一条可下载
        记录"的完整链路。目前调用方是服务预览的源码打包，其它平台侧产物
        也可以复用。

        Args:
            rel_name: 相对用户输出目录的路径（含 sessions/{id}/ 前缀），即 stored_name
            abs_path: 文件在本地磁盘上的绝对路径
        """
        size = 0
        try:
            size = os.path.getsize(abs_path)
        except OSError:
            pass
        return self.register_output(
            user_id=user_id,
            filename=rel_name,
            size=size,
            session_id=session_id,
            agent_id=agent_id,
        )

    # ---------- 查询 ----------

    def get_file(self, file_id: int) -> Optional[FileRecord]:
        return self.db.query(FileRecord).filter(FileRecord.id == file_id).first()

    def list_files(self, user_id: Optional[int] = None, kind: Optional[str] = None,
                   q: Optional[str] = None, session_id: Optional[int] = None,
                   agent_id: Optional[int] = None, deleted: bool = False) -> List[dict]:
        """列表查询（带 username / 会话标题 / Agent 名称的展示数据）

        user_id=None 表示不过滤（仅管理员允许这样调用，API 层把关）。
        deleted=False 只查正常文件；True 只查回收站文件。

        来源会话状态：session_deleted=True 表示会话已被删除（session_id 仍保留），
        此时 session_title 取快照，前端据此把"来源"置灰且不可点击；
        session_owner_id 为会话属主，供管理员判断能否预览他人会话。
        """
        query = self.db.query(FileRecord, User.username, ChatSession.title,
                              ChatSession.user_id, Agent.name)\
            .outerjoin(User, User.id == FileRecord.user_id)\
            .outerjoin(ChatSession, ChatSession.id == FileRecord.session_id)\
            .outerjoin(Agent, Agent.id == FileRecord.agent_id)
        if user_id is not None:
            query = query.filter(FileRecord.user_id == user_id)
        if kind:
            query = query.filter(FileRecord.kind == kind)
        if session_id is not None:
            query = query.filter(FileRecord.session_id == session_id)
        if agent_id is not None:
            query = query.filter(FileRecord.agent_id == agent_id)
        if q:
            query = query.filter(FileRecord.original_name.like(f"%{q}%"))
        if deleted:
            query = query.filter(FileRecord.deleted_at.isnot(None))
        else:
            query = query.filter(FileRecord.deleted_at.is_(None))
        rows = query.order_by(FileRecord.created_at.desc()).all()
        result = []
        for rec, username, live_title, session_owner, agent_name in rows:
            # 会话已删除：session_id 被置空（FK 约束）但 session_title 快照仍在
            session_deleted = rec.session_id is None and rec.session_title is not None
            result.append({
                "id": rec.id,
                "user_id": rec.user_id,
                "username": username,
                "kind": rec.kind,
                "original_name": rec.original_name,
                "stored_name": rec.stored_name,
                "size": rec.size or 0,
                "content_type": rec.content_type,
                "session_id": rec.session_id,
                "agent_id": rec.agent_id,
                "session_title": live_title or rec.session_title,
                "session_owner_id": session_owner,
                "session_deleted": session_deleted,
                "agent_name": agent_name,
                "created_at": rec.created_at,
                "deleted_at": rec.deleted_at,
            })
        return result

    # ---------- 操作 ----------

    def rename(self, file_id: int, new_name: str) -> Optional[FileRecord]:
        rec = self.get_file(file_id)
        if not rec:
            return None
        rec.original_name = new_name
        self.db.commit()
        self.db.refresh(rec)
        return rec

    def soft_delete(self, file_id: int) -> bool:
        rec = self.get_file(file_id)
        if not rec:
            return False
        rec.deleted_at = datetime.now()  # 北京时间，与 created_at 口径一致
        self.db.commit()
        return True

    def restore(self, file_id: int) -> bool:
        rec = self.get_file(file_id)
        if not rec:
            return False
        rec.deleted_at = None
        self.db.commit()
        return True

    def hard_delete(self, file_id: int) -> bool:
        """彻底删除：本地文件 + 数据库记录。"""
        rec = self.get_file(file_id)
        if not rec:
            return False
        path = self._disk_path(rec)
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass  # 磁盘已无文件也继续删记录
        self.db.delete(rec)
        self.db.commit()
        return True

    def read_content(self, rec: FileRecord) -> bytes:
        """读取文件字节（本地磁盘）。本地缺失时抛 FileNotFoundError。"""
        path = self._disk_path(rec)
        if not path or not os.path.exists(path):
            raise FileNotFoundError(f"本地无文件: {rec.stored_name}")
        with open(path, "rb") as f:
            return f.read()

    def _disk_path(self, rec: FileRecord) -> Optional[str]:
        """文件在磁盘上的绝对路径；stored_name 异常（穿越企图）时返回 None"""
        name = rec.stored_name or ""
        if not name or os.path.isabs(name) or ":" in name or name.startswith(("\\", "/")):
            return None
        parts = name.replace("\\", "/").split("/")
        if any(p in ("", ".", "..") for p in parts):
            return None
        if rec.kind == "upload":
            base = os.path.realpath(str(UPLOAD_DIR))
        elif rec.kind == "output":
            base = os.path.realpath(os.path.join(str(OUTPUT_DIR), f"user_{rec.user_id}"))
        else:
            return None
        path = os.path.realpath(os.path.join(base, *parts))
        if not path.startswith(base + os.sep):
            return None
        return path

    # ---------- 配额与统计 ----------

    def usage(self, user_id: int) -> dict:
        """单个用户的用量（不含回收站内文件）"""
        rows = self.db.query(
            func.count(FileRecord.id), func.coalesce(func.sum(FileRecord.size), 0)
        ).filter(
            FileRecord.user_id == user_id,
            FileRecord.deleted_at.is_(None),
        ).one()
        quota_bytes = settings.FILE_QUOTA_MB * 1024 * 1024
        return {"used_bytes": int(rows[1] or 0), "count": int(rows[0] or 0),
                "quota_bytes": quota_bytes}

    def check_quota(self, user_id: int, additional_bytes: int) -> bool:
        """上传前检查配额：True=允许"""
        u = self.usage(user_id)
        return u["used_bytes"] + additional_bytes <= u["quota_bytes"]

    def stats(self) -> dict:
        """全局统计（管理员视角）：平台文件总数与总用量。
        不再做逐用户 group_by 排行——用户量上千后该聚合会成为慢查询。"""
        total = self.db.query(
            func.count(FileRecord.id), func.coalesce(func.sum(FileRecord.size), 0)
        ).filter(FileRecord.deleted_at.is_(None)).one()
        return {
            "total_count": int(total[0] or 0),
            "total_bytes": int(total[1] or 0),
            "quota_mb_per_user": settings.FILE_QUOTA_MB,
        }
