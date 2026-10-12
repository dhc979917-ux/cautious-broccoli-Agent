"""任务运行期间的用户输入排队（延时发送）。

场景：Agent 任务正在运行时，用户继续输入/上传附件并点"发送"，内容不立即起新任务，
而是挂到该会话的 pending 队列；下一轮模型调用开始时（manager.chat_stream 的
ModelCallStartEvent 处）经 agent.observe() 注入模型上下文，同时落库为 user 消息。

协议语义：前端每次发送当前完整队列快照（queue_input），后端整体替换；
投递（drain）或 Esc 停止时一次性取出全部。

生命周期与 TaskManager 一致（内存态）：服务器重启会丢失，和"重启丢运行中任务"的
既有局限一致。
"""

import os
from typing import Dict, List


def normalize_item(raw) -> dict:
    """把前端传来的排队项规范成 {content, files:[{path,name,stored_name}]}。

    兼容文件对象的两种键名（path/file_path, name/original_name）与旧的纯路径字符串。
    """
    if not isinstance(raw, dict):
        raw = {}
    content = str(raw.get("content") or "").strip()
    files = []
    for f in raw.get("files") or []:
        if isinstance(f, dict):
            path = str(f.get("path") or f.get("file_path") or "")
            name = str(f.get("name") or f.get("original_name") or os.path.basename(path))
            stored = str(f.get("stored_name") or os.path.basename(path))
        else:
            path = str(f)
            name = os.path.basename(path)
            stored = name
        if path:
            files.append({"path": path, "name": name, "stored_name": stored})
    return {"content": content, "files": files}


def item_is_empty(item: dict) -> bool:
    return not item.get("content") and not item.get("files")


def normalize_items(raw) -> List[dict]:
    """批量规范化：前端发来的 items 列表 → 规范排队项列表（剔除空项）。"""
    return [it for it in (normalize_item(r) for r in (raw or [])) if not item_is_empty(it)]


def files_block(files: list) -> str:
    """附件说明文本（注入给模型的内容，与既有发送链路的格式保持一致）。"""
    if not files:
        return ""
    info = "\n\n[用户附带了以下文件]\n"
    for f in files:
        info += f"- {f['name']} (完整路径: {f['path']})\n"
    info += (
        "\n请使用对应的技能工具处理这些文件，输出结果请保存到 outputs 目录。\n"
        "路径请直接复制使用，不要凭记忆改写（文件名含长哈希，抄错会找不到文件）；"
        "若提示文件不存在，先 ls 该目录核对实际文件名，不要直接回复用户“没有附件”。\n"
    )
    return info


def item_attachments(item: dict) -> list:
    """落库/前端展示用的附件元信息（与既有 user 消息 metadata.attachments 同构）。"""
    return [{"name": f["name"], "stored_name": f["stored_name"]} for f in item.get("files") or []]


def item_agent_content(item: dict) -> str:
    """单个排队项注入给模型的完整文本（正文 + 附件说明）。"""
    return (item.get("content") or "") + files_block(item.get("files") or [])


def compose_injection_text(items: list) -> str:
    """把多个排队项拼成一条注入消息。"""
    parts = [item_agent_content(it) for it in items if not item_is_empty(it)]
    return "\n\n".join(p for p in parts if p)


# ==================== 会话级排队注册表（进程内内存态） ====================

_queues: Dict[int, List[dict]] = {}


def set_queue(session_id: int, items: list) -> List[dict]:
    """整体替换会话的排队快照（前端每次发全量）。空列表=清空。"""
    normalized = [it for it in (normalize_item(r) for r in (items or [])) if not item_is_empty(it)]
    if normalized:
        _queues[session_id] = normalized
    else:
        _queues.pop(session_id, None)
    return normalized


def peek(session_id: int) -> List[dict]:
    """只读查看当前排队内容（task-status 查询用）。"""
    return list(_queues.get(session_id, []))


def drain(session_id: int) -> List[dict]:
    """取出全部排队项并清空（投递/停止时调用）。"""
    return _queues.pop(session_id, [])


def clear(session_id: int) -> None:
    _queues.pop(session_id, None)
