# -*- coding: utf-8 -*-
"""OCR Skill — 经平台限权代理访问企业网关的视觉引擎。

本地 RapidOCR 只能"抄字"，对行军图、Q-time 柱状图、链路树形图、MAP 缺陷图
这类**图形语义**无能为力；VLM 能读出来。因此两条路并存：
有短期 OCR 凭证时优先 VLM，失败自动回落本地引擎。

沙箱只接收作用域为 OCR 的短期凭证，不持有企业网关主密钥。
"""
import time
from typing import Any, Optional

import requests

try:
    from ._config import VLM_BASE_URL, VLM_MODEL, VLM_PROMPT, VLM_TIMEOUT, vlm_api_key
    from ._loader import to_data_uri
except ImportError:  # 直接以脚本方式运行
    from _config import VLM_BASE_URL, VLM_MODEL, VLM_PROMPT, VLM_TIMEOUT, vlm_api_key
    from _loader import to_data_uri

# 图表理解提示词：要的是"这张图在说什么"，不是把坐标轴数字抄一遍
CHART_PROMPT = (
    "这是一张数据图表（可能是趋势图/柱状图/树形结构图/分布 MAP 等）。"
    "请按以下结构输出：\n"
    "1) 图表类型与坐标含义；\n"
    "2) 图中出现的全部文字标签（类别名、系列名、参数名、图例）；\n"
    "3) 可见的异常或集中趋势（哪个类别/系列/参数突出，数值大约多少）；\n"
    "4) 若图中无可读信息，直接说明。不要编造图中不存在的内容。"
)

_SESSION: Optional[requests.Session] = None


def _session() -> requests.Session:
    global _SESSION
    if _SESSION is None:
        _SESSION = requests.Session()
    return _SESSION


def is_available() -> tuple[bool, str]:
    """VLM 路径是否可用（只判 Key；出网失败由调用处回落处理）。"""
    if vlm_api_key() and VLM_BASE_URL:
        return True, ""
    return False, (
        "未配置限权 OCR 代理凭证，自动使用本地引擎。"
    )


def _extract_text(message: Any) -> str:
    """从 chat completion 的 message.content 里取文本（兼容 str 与分片 list 两种形态）。"""
    content = (message or {}).get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for piece in content:
            if isinstance(piece, dict):
                parts.append(str(piece.get("text") or ""))
            elif isinstance(piece, str):
                parts.append(piece)
        return "\n".join(p for p in parts if p).strip()
    return ""


def recognize(
    data: bytes,
    mode: str = "text",
    prompt: Optional[str] = None,
    model: Optional[str] = None,
    timeout: int = VLM_TIMEOUT,
    filename: str = "",
) -> dict[str, Any]:
    """把图片送给 VLM 读。

    Args:
        data: 图片二进制（调用方应已做尺寸归一）
        mode: "text"=照抄文字（默认提示词） / "chart"=图表语义理解
        prompt: 自定义提示词，给了就覆盖 mode 的默认值
        model: 模型名，默认取 _config.VLM_MODEL（qwen-vl-ocr）
        timeout: 单次请求超时秒数

    Returns:
        dict: {"engine", "model", "text", "elapsed_seconds", "usage"}

    Raises:
        RuntimeError: 无凭据、网络失败或接口返回错误
    """
    ok, reason = is_available()
    if not ok:
        raise RuntimeError(reason)

    instruction = prompt or (CHART_PROMPT if mode == "chart" else VLM_PROMPT)
    payload = {"image": to_data_uri(data, filename), "mode": mode}

    t0 = time.time()
    try:
        resp = _session().post(
            VLM_BASE_URL,
            headers={
                "Authorization": f"Bearer {vlm_api_key()}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=timeout,
        )
    except requests.RequestException as e:
        raise RuntimeError(f"VLM 请求失败: {type(e).__name__}: {e}") from e
    elapsed = round(time.time() - t0, 2)

    if resp.status_code != 200:
        raise RuntimeError(f"VLM 返回 HTTP {resp.status_code}: {resp.text[:300]}")

    try:
        body = resp.json()
    except ValueError as e:
        raise RuntimeError(f"VLM 响应不是 JSON: {resp.text[:200]}") from e

    choices = body.get("choices") or []
    text = _extract_text((choices[0] or {}).get("message")) if choices else ""
    if not text:
        raise RuntimeError(f"VLM 未返回文本内容: {str(body)[:300]}")

    return {
        "engine": "vlm",
        "model": body.get("model") or VLM_MODEL,
        "mode": mode,
        "text": text,
        "line_count": len([x for x in text.splitlines() if x.strip()]),
        "elapsed_seconds": elapsed,
        "usage": body.get("usage") or {},
    }
