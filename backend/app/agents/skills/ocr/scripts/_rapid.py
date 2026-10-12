# -*- coding: utf-8 -*-
"""OCR Skill — 本地离线引擎（RapidOCR / PP-OCR 模型 + onnxruntime）。

随沙箱镜像预装（docker/Dockerfile.skill），不需要外网、不消耗配额，
进程内首次构造约 0.6 秒、单张图识别约 1 秒，适合批量刷图。

注意：rapidocr-onnxruntime 依赖 opencv，slim 镜像里必须用
opencv-python-headless，否则会因缺 libGL.so.1 直接 import 失败。
"""
import time
from typing import Any, Optional

_ENGINE: Optional[Any] = None
_IMPORT_ERROR = ""


def is_available() -> tuple[bool, str]:
    """本地引擎是否可用；不可用时给出可直接照做的修复提示。"""
    global _IMPORT_ERROR
    if _ENGINE is not None:
        return True, ""
    try:
        import rapidocr_onnxruntime  # noqa: F401
        import cv2  # noqa: F401
        return True, ""
    except ImportError as e:
        _IMPORT_ERROR = str(e)
        missing = "cv2(opencv)" if "cv2" in str(e) else "rapidocr_onnxruntime"
        return False, (
            f"本地 OCR 引擎不可用（缺 {missing}: {e}）。"
            "沙箱镜像应已预装；若确实缺失，安装命令为 "
            "`pip install rapidocr-onnxruntime && pip uninstall -y opencv-python "
            "&& pip install opencv-python-headless`（必须用 headless 版，否则缺 libGL.so.1）。"
        )


def _engine() -> Any:
    """惰性构造并复用引擎实例（构造要加载三个 onnx 模型，别每张图都建一次）。"""
    global _ENGINE
    if _ENGINE is None:
        from rapidocr_onnxruntime import RapidOCR
        _ENGINE = RapidOCR()
    return _ENGINE


def recognize(data: bytes, detail: str = "text") -> dict[str, Any]:
    """识别图片二进制。

    Args:
        data: 图片二进制
        detail: "text"=只要整段文字；"layout"=附带每行的置信度与四点框坐标

    Returns:
        dict: {"engine", "text", "line_count", "blocks"?, "elapsed_seconds"}

    Raises:
        RuntimeError: 引擎不可用或识别过程出错
    """
    ok, reason = is_available()
    if not ok:
        raise RuntimeError(reason)

    t0 = time.time()
    try:
        raw, _elapse = _engine()(data)
    except Exception as e:
        raise RuntimeError(f"RapidOCR 识别失败: {type(e).__name__}: {e}") from e
    elapsed = round(time.time() - t0, 2)

    blocks: list[dict[str, Any]] = []
    lines: list[str] = []
    for item in raw or []:
        # RapidOCR 返回 [[四点框, 文本, 置信度], ...]
        try:
            box, text, score = item[0], item[1], float(item[2])
        except (IndexError, TypeError, ValueError):
            continue
        text = str(text).strip()
        if not text:
            continue
        lines.append(text)
        if detail == "layout":
            blocks.append({"text": text, "confidence": round(score, 4), "box": box})

    out: dict[str, Any] = {
        "engine": "rapid",
        "text": "\n".join(lines),
        "line_count": len(lines),
        "elapsed_seconds": elapsed,
    }
    if detail == "layout":
        out["blocks"] = blocks
    return out
