"""LLM 分类建议：喂标题+摘要给 qwen，从已有分类选/提新，返回建议（人工确认后写 DB，不改 md）。"""
import json, re
import requests
from app.config import settings
from app.services.model_gateway import endpoint, headers

COMPAT_URL = endpoint("chat/completions")


def _llm(system: str, user: str, model: str = None, temperature: float = 0.3) -> str:
    request_headers = headers()
    payload = {"model": model or settings.MODEL_NAME,
               "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
               "temperature": temperature}
    r = requests.post(COMPAT_URL, headers=request_headers, json=payload, timeout=60)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def classify_suggest(title: str, text: str, existing_categories) -> dict:
    cats = "、".join(existing_categories) if existing_categories else "（暂无，可新建）"
    sys_prompt = (
        "你是知识库分类助手。根据文档标题与摘要，从已有分类里选最贴切的一个；"
        "若都不合适可提一个新分类名（≤8 字）。只输出 JSON："
        '{"category":"分类名","reason":"一句话理由","alternatives":["备选1","备选2"]}'
    )
    user_prompt = f"已有分类：{cats}\n标题：{title}\n摘要：{(text or '')[:500]}"
    try:
        raw = _llm(sys_prompt, user_prompt)
    except Exception as e:
        return {"category": None, "reason": f"LLM 调用失败：{e}", "alternatives": []}
    m = re.search(r'\{.*\}', raw, re.S)
    if not m:
        return {"category": None, "reason": raw[:200], "alternatives": []}
    try:
        return json.loads(m.group(0))
    except Exception:
        return {"category": None, "reason": raw[:200], "alternatives": []}
