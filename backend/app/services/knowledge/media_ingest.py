"""多模态入库（阶段一：模态前置层）。

设计（与改造计划一致）：
- 模态前置转换：图像/音频在入库时先转成"文本表示 + 媒体引用"，
  产出的 Markdown 替身（frontmatter 带 modality/media_ref）交给现有
  MarkdownIndexer + 向量化管道——后续索引/检索/引用一行不用改；
- 源文件永不丢弃：媒体原件存档到 knowledge/{kb_id}/media/，md 是替身；
- 切分规则内建：图像替身不切（整篇一块，cite 能指回整图）；
  音频按时间窗口分节（时间戳即"可跳回播放"的锚点）；
- 全部走统一模型网关（OpenAI 兼容）：图像 = chat completions + image_url，
  音频 = /audio/transcriptions（whisper 兼容，verbose_json 带 segments）；
- MEDIA_INGEST_ENABLED 关闭时上传侧直接拒绝，行为与改造前一致。
"""

import asyncio
import base64
import logging
import mimetypes
import os
import shutil
from typing import Callable, List, Optional, Tuple

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# 支持的模态扩展名（小写，含点）
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".aac", ".wma"}
MEDIA_EXTS = IMAGE_EXTS | AUDIO_EXTS

_VLM_PROMPT = (
    "你是知识库的图像编目员。请用中文为这张图片生成检索友好的 Markdown，"
    "严格包含两个章节：\n"
    "## 图像描述\n（画面内容、场景、主体、颜色、构图，3~6 句；"
    "如果是图表/截图，说明图表类型和展示的数据趋势）\n"
    "## 画面文字\n（逐条抄录画面中可见的文字，没有则写「无」）\n"
    "只输出 Markdown 正文，不要输出代码块围栏。"
)


def modality_of(filename: str) -> Optional[str]:
    ext = os.path.splitext(filename or "")[1].lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in AUDIO_EXTS:
        return "audio"
    return None


def is_media(filename: str) -> bool:
    return modality_of(filename) is not None


def media_enabled() -> bool:
    return bool(getattr(settings, "MEDIA_INGEST_ENABLED", False))


def _media_dir(kb_dir: str) -> str:
    d = os.path.join(kb_dir, "media")
    os.makedirs(d, exist_ok=True)
    return d


def _archive_media(source_path: str, kb_dir: str, stored_name: str) -> str:
    """媒体原件存档（不丢弃本体，md 只是替身）。返回相对 vault 的引用。"""
    media_dir = _media_dir(kb_dir)
    target = os.path.join(media_dir, stored_name)
    if os.path.abspath(source_path) != os.path.abspath(target):
        shutil.copy2(source_path, target)
    return f"media/{stored_name}"


def _gateway_headers() -> dict:
    if not settings.AI_GATEWAY_API_KEY:
        raise RuntimeError("AI_GATEWAY_API_KEY 未配置")
    return {"Authorization": f"Bearer {settings.AI_GATEWAY_API_KEY}"}


def _endpoint(path: str) -> str:
    return settings.AI_GATEWAY_BASE_URL.rstrip("/") + "/" + path.lstrip("/")


# ---------------- 图像：VLM 描述 ----------------

async def _describe_image(path: str) -> str:
    """调用网关视觉模型（chat completions + image_url）生成替身正文。"""
    model = settings.MEDIA_VLM_MODEL
    if not model:
        raise RuntimeError("MEDIA_VLM_MODEL 未配置")
    ext = os.path.splitext(path)[1].lower().lstrip(".") or "png"
    mime = mimetypes.guess_type(path)[0] or f"image/{ext}"
    with open(path, "rb") as fh:
        data_uri = f"data:{mime};base64," + base64.b64encode(fh.read()).decode("ascii")
    payload = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": _VLM_PROMPT},
                {"type": "image_url", "image_url": {"url": data_uri}},
            ],
        }],
        "temperature": 0.2,
    }
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(_endpoint("chat/completions"), json=payload,
                                 headers=_gateway_headers())
        resp.raise_for_status()
        data = resp.json()
    return (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""


# ---------------- 音频：ASR 转写 ----------------

def _fmt_ts(seconds: float) -> str:
    seconds = max(0, int(seconds or 0))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


async def _transcribe_audio(path: str) -> Tuple[str, List[dict]]:
    """调用网关 ASR（whisper 兼容 /audio/transcriptions）。

    返回 (全文, segments)；segments 形如
    [{"start": 0.0, "end": 5.2, "text": "..."}]，无分段信息时为空列表
    （调用方按固定窗口估计切分）。
    """
    model = settings.MEDIA_ASR_MODEL
    if not model:
        raise RuntimeError("MEDIA_ASR_MODEL 未配置")
    mime = mimetypes.guess_type(path)[0] or "audio/mpeg"
    async with httpx.AsyncClient(timeout=600) as client:
        with open(path, "rb") as fh:
            resp = await client.post(
                _endpoint("audio/transcriptions"),
                headers=_gateway_headers(),
                data={"model": model, "response_format": "verbose_json"},
                files={"file": (os.path.basename(path), fh, mime)},
            )
    resp.raise_for_status()
    data = resp.json()
    text = data.get("text") or ""
    segments = [s for s in (data.get("segments") or [])
                if isinstance(s, dict) and s.get("text")]
    return text, segments


def _compose_audio_md(segments: List[dict], total_duration_hint: Optional[float],
                      section_seconds: int) -> str:
    """按时间窗口分节合成转写正文（时间戳 = 检索命中后的回放锚点）。"""
    if not segments:
        return "（转写为空）"
    section_seconds = max(30, int(section_seconds or 120))
    lines: List[str] = []
    window_start: Optional[float] = None
    window_end: float = 0.0
    buf: List[str] = []

    def _flush():
        if buf and window_start is not None:
            lines.append(f"\n## [{_fmt_ts(window_start)}-{_fmt_ts(window_end)}]\n")
            lines.append("".join(buf))
        buf.clear()

    for seg in segments:
        start = float(seg.get("start") or 0)
        end = float(seg.get("end") or start)
        if window_start is None:
            window_start = start
        if start - window_start >= section_seconds:
            _flush()
            window_start = start
        window_end = max(window_end, end)
        speaker = seg.get("speaker")
        prefix = f"[{speaker}] " if speaker else ""
        buf.append(f"{prefix}{str(seg.get('text') or '').strip()} ")
    _flush()
    if total_duration_hint:
        lines.append(f"\n（录音总时长约 {_fmt_ts(total_duration_hint)}）")
    return "".join(lines).strip()


def _estimate_sections(text: str, section_seconds: int) -> str:
    """无分段信息时的兜底：按句均匀估算时间轴（保底可用）。"""
    import re as _re
    parts = [p.strip() for p in _re.split(r"(?<=[。！？.!?\n])", text or "") if p.strip()]
    if not parts:
        return text or "（转写为空）"
    per_section = max(1, len(parts) // max(1, len(parts) // 4 + 1))
    lines = []
    for i in range(0, len(parts), per_section * 2):
        est_start = i * section_seconds // max(1, per_section * 2)
        est_end = est_start + section_seconds
        lines.append(f"\n## [{_fmt_ts(est_start)}-{_fmt_ts(est_end)}]（估算）\n")
        lines.append("".join(parts[i:i + per_section * 2]))
    return "".join(lines).strip()


# ---------------- 入口：媒体 → Markdown 替身 ----------------

async def convert_media_to_md_async(
    *,
    source_path: str,
    vault_dir: str,
    original_filename: str,
    stored_name: str,
    kb_id: int,
    on_progress: Optional[Callable] = None,
    should_cancel: Optional[Callable] = None,
) -> str:
    """把媒体文件转成 Markdown 替身，返回 md 路径。

    与 SourceToMdService.convert_to_md_async 同签名约定，可无缝替换调用。
    """
    modality = modality_of(original_filename)
    if modality is None:
        raise ValueError(f"不支持的媒体类型: {original_filename}")

    def _report(pct, msg):
        if on_progress:
            try:
                on_progress(pct, msg)
            except Exception:
                pass

    _report(8, "存档媒体原件")
    media_ref = _archive_media(source_path, vault_dir, stored_name)
    if should_cancel and should_cancel():
        raise asyncio.CancelledError()

    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    size_kb = int(os.path.getsize(source_path) / 1024) if os.path.exists(source_path) else 0

    if modality == "image":
        _report(20, "视觉模型识别图像内容")
        body = await _describe_image(source_path)
        if should_cancel and should_cancel():
            raise asyncio.CancelledError()
        front = (
            "---\n"
            f"modality: image\n"
            f"media_ref: {media_ref}\n"
            f"source_file: {original_filename}\n"
            f"size_kb: {size_kb}\n"
            f"indexed: {today}\n"
            "---\n"
        )
        title = f"图像笔记：{os.path.splitext(original_filename)[0]}"
        md = f"{front}\n# {title}\n\n{body.strip()}\n"
    else:
        _report(20, "语音转写中（长音频可能需要几分钟）")
        text, segments = await _transcribe_audio(source_path)
        if should_cancel and should_cancel():
            raise asyncio.CancelledError()
        _report(70, "按时间轴分节")
        if segments:
            body = _compose_audio_md(segments, None, settings.AUDIO_SECTION_SECONDS)
        else:
            body = _estimate_sections(text, settings.AUDIO_SECTION_SECONDS)
        front = (
            "---\n"
            f"modality: audio\n"
            f"media_ref: {media_ref}\n"
            f"source_file: {original_filename}\n"
            f"size_kb: {size_kb}\n"
            f"segmented: {'true' if segments else 'estimated'}\n"
            f"indexed: {today}\n"
            "---\n"
        )
        title = f"录音转写：{os.path.splitext(original_filename)[0]}"
        md = f"{front}\n# {title}\n\n{body.strip()}\n"

    _report(78, "写入 Markdown 替身")
    base = os.path.splitext(os.path.basename(original_filename))[0]
    safe = "".join(c if c not in '\\/:*?"<>|' else "_" for c in base).strip() or "media"
    # 与 source_to_md 命名风格一致：避免与已有笔记重名
    md_name = f"{safe}.media.md"
    md_path = os.path.join(vault_dir, md_name)
    n = 1
    while os.path.exists(md_path):
        md_path = os.path.join(vault_dir, f"{safe}-{n}.media.md")
        n += 1
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(md)
    _report(80, "媒体替身生成完成")
    return md_path
