"""Bounded multi-pass memory summarization without dropping input text."""

from typing import Awaitable, Callable, Optional


def batch_material(parts: list[str], limit: int) -> list[str]:
    batches = []
    current = []
    size = 0
    for part in parts:
        for offset in range(0, len(part), limit):
            piece = part[offset:offset + limit]
            if size + len(piece) > limit and current:
                batches.append("\n\n".join(current))
                current = []
                size = 0
            current.append(piece)
            size += len(piece) + 2
    if current:
        batches.append("\n\n".join(current))
    return batches


async def fold_structured(system: str, lead: str, parts: list[str], model_cls,
                          generate: Callable[..., Awaitable[Optional[dict]]],
                          *, limit: int = 18000, worth_field: bool = False) -> Optional[dict]:
    """Summarize every segment, then recursively merge; return None on any failure."""
    remaining = [part for part in parts if part.strip()]
    has_worthy_part = False
    for _ in range(8):
        batches = batch_material(remaining, limit)
        if not batches:
            return {"worth": False, "content": ""} if worth_field else None
        if len(batches) == 1:
            result = await generate(system=system, user=f"{lead}\n\n{batches[0]}", model_cls=model_cls)
            if result is None:
                return None
            if worth_field and result.get("worth") is False:
                return None if has_worthy_part else result
            return result if str(result.get("content") or "").strip() else None
        summaries = []
        for index, batch in enumerate(batches, 1):
            result = await generate(
                system=system,
                user=f"{lead}\n第 {index}/{len(batches)} 部分，只总结当前部分，不遗漏关键事实。\n\n{batch}",
                model_cls=model_cls,
            )
            if result is None:
                return None
            if worth_field and result.get("worth") is False:
                continue
            if worth_field:
                has_worthy_part = True
            content = str(result.get("content") or "").strip()
            if not content:
                return None
            summaries.append(f"## 第 {index} 部分\n{content}")
        if not summaries:
            return {"worth": False, "content": ""} if worth_field else None
        if sum(map(len, summaries)) >= sum(map(len, batches)):
            return None
        remaining = summaries
    return None
