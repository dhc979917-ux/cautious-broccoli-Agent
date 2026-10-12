"""Progress and cancellation state for knowledge document ingestion.

The tracker is intentionally transport-free: ingestion code only reports stage
updates, while the API layer decides how to expose them (currently SSE).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional


class KnowledgeImportCancelled(Exception):
    """Raised when a user requests cancellation of an upload batch."""


CancelCheck = Callable[[], bool]


@dataclass
class DocumentProgress:
    id: int
    filename: str
    status: str = "queued"
    stage: str = "排队中"
    percent: int = 0
    detail: str = ""
    error: Optional[str] = None


@dataclass
class ProgressReporter:
    doc_id: int
    tracker: "KnowledgeUploadProgressTracker"
    start_percent: int = 0
    end_percent: int = 100

    def update(
        self,
        stage: str,
        percent: int,
        detail: str = "",
        status: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        self.report(stage, percent, detail, status, error)
        self.check_cancel()

    def report(
        self,
        stage: str,
        percent: int,
        detail: str = "",
        status: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        mapped = self.start_percent + max(0, min(100, percent)) * (
            self.end_percent - self.start_percent
        ) // 100
        self.tracker.update_doc(
            self.doc_id,
            stage=stage,
            percent=mapped,
            detail=detail,
            status=status,
            error=error,
        )
        # Check cancellation after every progress report so that a cancel
        # request interrupts long-running operations that only call report()
        # (not update()), such as the LLM streaming on_chunk callback.
        self.check_cancel()
    def check_cancel(self) -> None:
        if self.tracker.cancel_requested:
            raise KnowledgeImportCancelled()

    def child(self, start_percent: int, end_percent: int) -> "ProgressReporter":
        child_start = self.start_percent + start_percent * (self.end_percent - self.start_percent) // 100
        child_end = self.start_percent + end_percent * (self.end_percent - self.start_percent) // 100
        return ProgressReporter(
            doc_id=self.doc_id,
            tracker=self.tracker,
            start_percent=child_start,
            end_percent=child_end,
        )


@dataclass
class KnowledgeUploadProgressTracker:
    batch_id: str
    documents: dict[int, DocumentProgress]
    cancel_requested: bool = False
    _subscribers: set[asyncio.Queue] = field(default_factory=set, repr=False)

    @classmethod
    def from_documents(cls, batch_id: str, docs: Iterable) -> "KnowledgeUploadProgressTracker":
        terminal = {"ready", "converted", "failed", "canceled"}
        stage_label = {
            "uploading": "排队中",
            "parsing": "解析中",
            "converting": "转换中",
            "indexing": "向量化",
            "ready": "完成",
            "converted": "转换完成",
            "failed": "失败",
            "canceled": "已取消",
        }
        documents: dict[int, DocumentProgress] = {}
        for doc in docs:
            if isinstance(doc, dict):
                doc_id = doc["id"]
                filename = doc["filename"]
                status = doc.get("status") or "queued"
                error = doc.get("error")
            else:
                doc_id = doc.id
                filename = doc.filename
                status = doc.status or "queued"
                error = doc.error
            done = status in terminal
            documents[doc_id] = DocumentProgress(
                id=doc_id,
                filename=filename,
                status=status,
                stage=stage_label.get(status, status),
                percent=100 if done else 0,
                error=error,
            )
        return cls(batch_id=batch_id, documents=documents)

    def reporter(self, doc_id: int) -> ProgressReporter:
        if doc_id not in self.documents:
            raise KeyError(f"document {doc_id} is not in progress batch {self.batch_id}")
        return ProgressReporter(doc_id=doc_id, tracker=self)

    def update_doc(
        self,
        doc_id: int,
        *,
        stage: str,
        percent: int,
        detail: str = "",
        status: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        state = self.documents.get(doc_id)
        if state is None:
            return
        state.stage = stage
        state.percent = max(state.percent, max(0, min(100, percent)))
        state.detail = detail
        state.error = error
        if status:
            state.status = status
        self._publish()

    def request_cancel(self) -> None:
        if self.cancel_requested:
            return
        self.cancel_requested = True
        terminal = {"ready", "converted", "failed", "canceled"}
        for state in self.documents.values():
            if state.status not in terminal:
                state.status = "canceling"
                state.stage = "取消中"
                state.detail = "正在停止当前处理"
        self._publish()

    def retry_document(self, doc) -> None:
        """Reset one terminal document for a retry while preserving the batch."""
        terminal = {"ready", "converted", "failed", "canceled"}
        if all(state.status in terminal for state in self.documents.values()):
            self.cancel_requested = False
        if isinstance(doc, dict):
            doc_id = doc["id"]
        else:
            doc_id = doc.id
        state = self.documents.get(doc_id)
        if state is None:
            return
        state.status = "parsing"
        state.stage = "重试中"
        state.percent = 0
        state.detail = "已重新加入处理队列"
        state.error = None
        self._publish()

    def snapshot(self) -> dict:
        documents = [self._doc_snapshot(state) for state in self.documents.values()]
        terminal = {"ready", "converted", "failed", "canceled"}
        total = len(documents)
        done = sum(1 for d in documents if d["status"] in ("ready", "converted"))
        failed = sum(1 for d in documents if d["status"] == "failed")
        canceled = sum(1 for d in documents if d["status"] == "canceled")
        pending = sum(1 for d in documents if d["status"] not in terminal)
        percent = round(sum(d["percent"] for d in documents) / total) if total else 0
        return {
            "batch_id": self.batch_id,
            "cancel_requested": self.cancel_requested,
            "overall": {
                "total": total,
                "done": done,
                "failed": failed,
                "canceled": canceled,
                "pending": pending,
                "percent": percent,
            },
            "documents": documents,
        }

    async def subscribe(self, heartbeat_interval: float = 15.0):
        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers.add(queue)
        try:
            yield self.snapshot()
            while True:
                try:
                    yield await asyncio.wait_for(queue.get(), timeout=heartbeat_interval)
                except asyncio.TimeoutError:
                    yield None
        finally:
            self._subscribers.discard(queue)

    def _doc_snapshot(self, state: DocumentProgress) -> dict:
        return {
            "id": state.id,
            "filename": state.filename,
            "status": state.status,
            "stage": state.stage,
            "percent": state.percent,
            "detail": state.detail,
            "error": state.error,
        }

    def _publish(self) -> None:
        snapshot = self.snapshot()
        for queue in list(self._subscribers):
            queue.put_nowait(snapshot)


class KnowledgeUploadProgressRegistry:
    """Process-local registry; terminal snapshots remain available for reconnects."""

    def __init__(self, max_batches: int = 128):
        self._trackers: dict[str, KnowledgeUploadProgressTracker] = {}
        self._max_batches = max_batches

    def register(self, tracker: KnowledgeUploadProgressTracker) -> None:
        self._trackers[tracker.batch_id] = tracker
        if len(self._trackers) > self._max_batches:
            overflow = list(self._trackers)[: len(self._trackers) - self._max_batches]
            for batch_id in overflow:
                del self._trackers[batch_id]

    def get(self, batch_id: str) -> Optional[KnowledgeUploadProgressTracker]:
        return self._trackers.get(batch_id)

    def get_or_create(self, batch_id: str, docs: Iterable) -> KnowledgeUploadProgressTracker:
        tracker = self._trackers.get(batch_id)
        if tracker is None:
            tracker = KnowledgeUploadProgressTracker.from_documents(batch_id, docs)
            self.register(tracker)
        return tracker


knowledge_upload_progress_registry = KnowledgeUploadProgressRegistry()
