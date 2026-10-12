"""Plain-Python regression for knowledge upload progress and cancellation."""

import asyncio
from pathlib import Path
import uuid
import sys

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from app.services.knowledge.progress import (  # noqa: E402
    KnowledgeImportCancelled,
    KnowledgeUploadProgressTracker,
)
from app.services.knowledge.source_to_md_service import SourceToMdService  # noqa: E402
from app.services.knowledge import source_to_md_service as source_module  # noqa: E402


def test_progress_and_cancel() -> None:
    tracker = KnowledgeUploadProgressTracker.from_documents(
        "batch-test",
        [{"id": 1, "filename": "demo.txt", "status": "parsing", "error": None}],
    )
    reporter = tracker.reporter(1)

    reporter.update("解析中", 20, "抽取文本", status="parsing")
    assert tracker.documents[1].percent == 20

    child = reporter.child(50, 100)
    child.report("向量化", 50, "生成 embeddings")
    assert tracker.documents[1].percent == 75

    tracker.request_cancel()
    try:
        reporter.update("解析中", 80, "should cancel")
    except KnowledgeImportCancelled:
        pass
    else:
        raise AssertionError("reporter.update should raise after cancellation")

    tracker.update_doc(1, stage="已取消", percent=100, status="canceled")
    snapshot = tracker.snapshot()
    assert snapshot["overall"]["canceled"] == 1
    assert snapshot["overall"]["pending"] == 0
    assert snapshot["documents"][0]["percent"] == 100

    tracker.retry_document({"id": 1, "filename": "demo.txt"})
    snapshot = tracker.snapshot()
    assert tracker.cancel_requested is False
    assert snapshot["documents"][0]["status"] == "parsing"
    assert snapshot["documents"][0]["percent"] == 0


async def test_subscription() -> None:
    tracker = KnowledgeUploadProgressTracker.from_documents(
        "stream-test",
        [{"id": 2, "filename": "stream.txt", "status": "parsing", "error": None}],
    )
    events = []
    async for event in tracker.subscribe(heartbeat_interval=0.01):
        events.append(event)
        tracker.update_doc(2, stage="完成", percent=100, status="ready")
        if len(events) == 2:
            break
    assert events[0]["overall"]["pending"] == 1
    assert events[1]["documents"][0]["status"] == "ready"


async def test_source_conversion_cancel() -> None:
    try:
        await SourceToMdService().convert_to_md_async(
            source_path="unused.txt",
            vault_dir="unused",
            original_filename="unused.txt",
            stored_name="unused.txt",
            should_cancel=lambda: True,
        )
    except KnowledgeImportCancelled:
        pass
    else:
        raise AssertionError("source conversion should honor cancellation")


async def test_source_conversion_progress_callback() -> None:
    base_dir = Path(__file__).parent
    token = uuid.uuid4().hex
    filename = f"_progress_source_{token}.txt"
    source_path = base_dir / filename
    source_path.write_text("# Demo\n\nProgress callback regression.", encoding="utf-8")

    async def fake_optimize(raw_md, existing_titles, filename, on_progress, should_cancel):
        assert "Progress callback" in raw_md
        assert existing_titles == ["Existing"]
        assert filename.startswith("_progress_source_") and filename.endswith(".txt")
        if on_progress:
            on_progress("智能改写", 60, "mock rewrite")
        assert should_cancel is not None and not should_cancel()
        return {"title": "Demo", "summary": "", "tags": [], "body": raw_md}

    service = SourceToMdService()
    service._llm_optimize_async = fake_optimize  # type: ignore[method-assign]
    events = []
    try:
        md_path = await service.convert_to_md_async(
            source_path=str(source_path),
            vault_dir=str(base_dir),
            original_filename=filename,
            stored_name=filename,
            existing_titles=["Existing"],
            kb_id=1,
            on_progress=lambda stage, percent, detail: events.append((stage, percent, detail)),
            should_cancel=lambda: False,
        )
        assert Path(md_path).is_file()
        assert any(stage == "抽取正文" and percent == 12 for stage, percent, _ in events)
        assert any(stage == "智能改写" and percent == 60 for stage, percent, _ in events)
        assert any(stage == "生成笔记" and percent == 72 for stage, percent, _ in events)
    finally:
        source_path.unlink(missing_ok=True)
        (base_dir / f"_progress_source_{token}.md").unlink(missing_ok=True)
        (base_dir / f"_progress_source_{token}.raw.txt").unlink(missing_ok=True)


async def test_llm_stream_progress_callback() -> None:
    service = SourceToMdService()

    async def fake_llm(system, user, model=None, temperature=0.3, on_chunk=None):
        assert "RAW CONTENT" in user
        for text in ['{"body":"', " streamed ", 'body"}']:
            if on_chunk:
                on_chunk(text)
        return '{"body":" streamed body"}'

    source_module._llm_async = fake_llm
    events = []
    result = await service._rewrite_part_async(
        "0123456789",
        "(none)",
        "demo.txt",
        True,
        on_progress=lambda stage, percent, detail: events.append((stage, percent, detail)),
    )
    assert result["body"] == " streamed body"
    assert len(events) >= 3
    assert events[0][1] < events[-1][1]
    assert all(stage == "智能改写" for stage, _, _ in events)


if __name__ == "__main__":
    test_progress_and_cancel()
    asyncio.run(test_subscription())
    asyncio.run(test_source_conversion_cancel())
    asyncio.run(test_source_conversion_progress_callback())
    asyncio.run(test_llm_stream_progress_callback())
    print("knowledge progress regression: PASS")
