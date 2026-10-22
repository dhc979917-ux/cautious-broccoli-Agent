"""Cross-encoder rerank（检索芯强化 · P0）。

为什么需要：两路召回（向量 + 词面）只能给出"粗排"——向量分和 BM25 分
量纲不同，融合排序的精度天花板低。重排序器把（查询, 候选文本）成对送进
交叉编码器逐条精打分，是 RAG 精度提升最猛的单点改动。

设计约束（与全项目风格一致）：
- 可选依赖：KB_RERANK_ENABLED 关闭或网关调用失败时，静默回退原始排序，
  检索永远不因 rerank 挂掉而中断；
- 走统一模型网关（AI_GATEWAY_BASE_URL，Cohere/Jina 风格的 POST /rerank），
  不自托管模型、不新增基础设施；
- 只在出口做一层：KnowledgeService.search 收拢完多库候选后调用。
"""

import logging
from typing import List, Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# 单次 rerank 的候选上限：再多收益递减且延迟上升
MAX_CANDIDATES = 32


def available() -> bool:
    """rerank 是否启用（配置开关 + 网关地址 + 模型名三齐）。"""
    return bool(
        getattr(settings, "KB_RERANK_ENABLED", False)
        and settings.AI_GATEWAY_BASE_URL
        and settings.AI_GATEWAY_API_KEY
        and getattr(settings, "KB_RERANK_MODEL", "")
    )


async def rerank_scores(query: str, texts: List[str]) -> Optional[List[float]]:
    """对（query, texts）逐条精打分。

    返回与 texts 等长的分数列表；任何失败返回 None（调用方回退原排序）。
    """
    if not available() or not query.strip() or not texts:
        return None
    texts = texts[:MAX_CANDIDATES]
    payload = {
        "model": settings.KB_RERANK_MODEL,
        "query": query,
        "documents": [t[:4000] for t in texts],
        "top_n": len(texts),
        "return_documents": False,
    }
    headers = {"Authorization": f"Bearer {settings.AI_GATEWAY_API_KEY}"}
    url = settings.AI_GATEWAY_BASE_URL.rstrip("/") + "/rerank"
    try:
        async with httpx.AsyncClient(timeout=settings.KB_RERANK_TIMEOUT_SECONDS) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        logger.warning(f"[rerank] 网关调用失败，回退原排序: {e}")
        return None
    # 标准返回形如 {"results": [{"index": 0, "relevance_score": 0.98}, ...]}
    results = data.get("results") or data.get("data") or []
    if not isinstance(results, list) or not results:
        return None
    scores = [0.0] * len(texts)
    for item in results:
        try:
            idx = int(item["index"])
            if 0 <= idx < len(scores):
                scores[idx] = float(item.get("relevance_score") or item.get("score") or 0.0)
        except (KeyError, TypeError, ValueError):
            continue
    return scores


async def apply_rerank(query: str, results: list, top_k: int) -> list:
    """就地重排 KnowledgeService.search 的结果列表（dict 带 text/score 键）。

    rerank 不可用/失败时：按原 score 排序返回 top_k（行为与改造前一致）。
    """
    if not results:
        return results
    texts = [str(r.get("text") or "") for r in results]
    scores = await rerank_scores(query, texts)
    if scores is None:
        return sorted(results, key=lambda r: r.get("score", 0), reverse=True)[:top_k]
    reranked = sorted(
        zip(scores, range(len(results))),
        key=lambda pair: pair[0], reverse=True,
    )
    out = []
    for _score, idx in reranked[:top_k]:
        item = dict(results[idx])
        # 保留原融合分供调试，rerank 分进 score（引用展示口径）
        item["fusion_score"] = item.get("score")
        item["score"] = round(float(_score), 4)
        out.append(item)
    return out
