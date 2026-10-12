"""User-requested summary compaction, independent of the Agent execution loop."""

import asyncio
import json
import time
import uuid
from datetime import datetime

from agentscope.message import Msg
from sqlalchemy.orm import Session

from app.agents.manager import agent_manager
from app.config import settings
from app.models.message import Message
from app.models.session import Session as ChatSession
from app.models.session_context import SessionContext
from app.services.model_gateway import chat_completion

NOTICE = "⚠ 提醒：过长的对话线程和多次压缩可能会导致模型准确性下降。尽可能开启新线程，以保持线程简短且目标明确。"
_SUMMARY_INSTRUCTION = (
    "请压缩以下历史对话，保留用户意图、当前目标、已作出的决定、重要事实、"
    "文件路径/产物、未解决问题和接下来的步骤。禁止编造；不要保留冗长的工具原始输出。"
    "只返回结构清晰的中文摘要，不要向用户回复。"
)


def _prompt_entry(message: dict) -> str:
    content = json.dumps(message.get("content", ""), ensure_ascii=False, default=str)
    limit = 14000 if message.get("role") == "user" else 2200
    if len(content) > limit:
        content = content[:limit - 1000] + " …[较长内容略]… " + content[-900:]
    return f"[{message.get('role', 'unknown')}] {content}"


async def compact_session(db: Session, session: ChatSession, model: str) -> dict:
    agent_manager.persist_session_context(session.id, db)
    row = db.get(SessionContext, session.id)
    if row is None or not row.state_json:
        return {"status": "skipped", "detail": "此会话没有需要压缩的上下文"}
    original_state = row.state_json
    payload = json.loads(original_state)
    context = payload.get("context") or []
    turns = [index for index, msg in enumerate(context) if msg.get("role") == "user"]
    if len(turns) < 2:
        return {"status": "skipped", "detail": "最近对话较短，无需压缩"}
    boundary = turns[-3] if len(turns) >= 4 else turns[-1]
    older, recent = context[:boundary], context[boundary:]
    if len(older) < 2:
        return {"status": "skipped", "detail": "最近对话较短，无需压缩"}
    recent_messages = [Msg.model_validate(msg) for msg in recent]
    history = [_prompt_entry(msg) for msg in older]
    summary = str(payload.get("summary") or "")
    token_input = token_output = token_cached = 0
    started = time.monotonic()
    chunk: list[str] = []
    chunk_size = 0
    chunks: list[str] = []
    for entry in history:
        if chunk and chunk_size + len(entry) > 48000:
            chunks.append("\n".join(chunk))
            chunk, chunk_size = [], 0
        chunk.append(entry)
        chunk_size += len(entry)
    if chunk:
        chunks.append("\n".join(chunk))
    for text in chunks:
        if len(summary) > 12000:
            summary = summary[:6000] + "\n…\n" + summary[-6000:]
        reply = await asyncio.to_thread(
            chat_completion,
            [{"role": "system", "content": _SUMMARY_INSTRUCTION},
             {"role": "user", "content": f"既有摘要：\n{summary}\n\n新增历史：\n{text}"}],
            model, 120,
        )
        generated = reply["choices"][0]["message"].get("content")
        if not isinstance(generated, str) or not generated.strip():
            raise ValueError("模型未返回有效摘要")
        summary = generated.strip()
        usage = reply.get("usage") or {}
        token_input += int(usage.get("prompt_tokens") or 0)
        token_output += int(usage.get("completion_tokens") or 0)
        token_cached += int((usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)

    db.refresh(row)
    if row.state_json != original_state:
        raise RuntimeError("会话上下文已更新，请重试压缩")
    payload["summary"] = summary
    payload["context"] = recent
    new_state = json.dumps(payload, ensure_ascii=False)
    if len(new_state.encode("utf-8")) > agent_manager.MAX_CONTEXT_JSON_BYTES:
        raise ValueError("近期对话仍超出上下文存储上限，建议开启新线程")
    run_id = f"compact-{uuid.uuid4().hex}"
    message = Message(
        session_id=session.id, role="agent", content=NOTICE,
        metadata_json=json.dumps({
            "run_id": run_id,
            "token_usage": {"input_tokens": token_input, "output_tokens": token_output,
                            "cache_input_tokens": token_cached},
            "elapsed_seconds": int(time.monotonic() - started),
            "context_compaction": True,
        }, ensure_ascii=False),
    )
    row.state_json = new_state
    session.updated_at = datetime.now()
    db.add(message)
    db.commit()
    for cache_key, agent in agent_manager.agents_cache.items():
        if agent_manager._key_sessions.get(cache_key) == session.id:
            agent.state.summary = summary
            agent.state.context = [Msg.model_validate(msg) for msg in recent]
    return {"status": "completed", "message_id": message.id, "usage": {"input_tokens": token_input,
            "output_tokens": token_output, "cache_input_tokens": token_cached}}
