"""文档内嵌图片：抽取存档 + VLM 理解 + 占位符展开。

PDF/Word/PPT/Excel 图文混排文档的统一图片通道（方案 D1~D6）：
- 抽取（source_to_md_service 各 _xxx_to_md 钩子传入 ImageSink）：
  图片字节按 MD5 去重（logo/水印只存一次）→ 存 vault/media/{md5}.{ext}
  → 原位置插入占位符 <!-- IMG:media/xxx.png -->；
- 理解：走统一网关视觉模型（MEDIA_VLM_MODEL，同 media_ingest 通道），
  每图 1 次调用产出「图像描述 + 画面文字」，失败降级为空描述（D4：
  图片照存、不阻塞入库）；
- 展开（expand_placeholders）：
  · 笔记 md：![图像](media/x.png) + 引用块描述（阅读可见，Obsidian 兼容）
  · 旁挂 .raw.txt：纯文本块（进入分句切块 → embedding/FTS，可被检索命中）
  · LLM 改写丢失的占位符统一追加文末「附图」，保证图片不丢。

占位符设计约束：
- 不是 [[wikilink]] 语法，MarkdownIndexer 不会误解析成笔记链接；
- 行长 >50 字符（md5 32 hex），raw_cleaner 的跨页重复行过滤（<50）不会误删；
- HTML 注释形式，LLM 改写 prompt 明确要求原位保留。
"""

import base64
import hashlib
import logging
import mimetypes
import os
import re
from typing import Callable, Optional

import requests

from app.config import settings

logger = logging.getLogger(__name__)

PLACEHOLDER_RE = re.compile(r"<!--\s*IMG:([^>\s]+)\s*-->")
# 图内文字理解提示词：描述 + 抄录两段，均为检索友好文本
_VLM_PROMPT = (
    "你是企业知识库的图像理解助手。请用中文理解这张来自文档的图片，"
    "严格输出两个章节：\n"
    "## 图像描述\n（图表类型、画面内容、展示的数据/趋势/结论，3~6 句；"
    "重点提炼检索时有价值的信息）\n"
    "## 画面文字\n（逐条抄录图中可见的文字；表格用 Markdown 表格表示；"
    "没有则写「无」）\n"
    "只输出 Markdown 正文，不要输出代码块围栏。"
)
# PDF 图片块的 bbox 小于该边长（pt）视为图标/装饰，跳过
MIN_BBOX_PT = 50
# 小于该字节数的图（pptx/docx 无 bbox 可用）视为图标
MIN_BYTES = 512
# 超过该字节数的图只保存不送 VLM（防单图过大撑爆 base64 请求）
VLM_MAX_BYTES = 8 * 1024 * 1024
_IMG_EXTS = {"png", "jpg", "jpeg", "gif", "webp", "bmp", "tif", "tiff", "jpx", "jp2"}


def extract_enabled() -> bool:
    return bool(settings.DOC_IMAGE_EXTRACT_ENABLED)


def vlm_enabled() -> bool:
    return bool(
        settings.DOC_IMAGE_VLM_ENABLED
        and settings.AI_GATEWAY_API_KEY
        and settings.AI_GATEWAY_BASE_URL
        and settings.MEDIA_VLM_MODEL
    )


def make_image_sink(vault_dir: Optional[str], on_image: Optional[Callable[[int], None]] = None):
    """工厂：未启用/无 vault 时返回 None（调用方零成本跳过）。"""
    if not extract_enabled() or not vault_dir:
        return None
    return ImageSink(vault_dir, on_image=on_image)


def describe_image_bytes(data: bytes, ext: str) -> str:
    """网关视觉模型理解单图（同步 requests；在 to_thread 内运行）。

    任何失败返回空串（D4：不阻塞入库，图片照存）。"""
    try:
        mime = mimetypes.guess_type(f"x.{ext}")[0] or "image/png"
        data_uri = f"data:{mime};base64," + base64.b64encode(data).decode("ascii")
        url = settings.AI_GATEWAY_BASE_URL.rstrip("/") + "/chat/completions"
        payload = {
            "model": settings.MEDIA_VLM_MODEL,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": _VLM_PROMPT},
                    {"type": "image_url", "image_url": {"url": data_uri}},
                ],
            }],
            "temperature": 0.2,
        }
        resp = requests.post(
            url,
            headers={"Authorization": f"Bearer {settings.AI_GATEWAY_API_KEY}"},
            json=payload,
            timeout=180,
        )
        resp.raise_for_status()
        body = resp.json()
        return ((body.get("choices") or [{}])[0]
                .get("message", {}).get("content") or "").strip()
    except Exception as e:
        logger.warning(f"[doc_images] VLM 理解失败（降级为无描述）: {e}")
        return ""


class ImageSink:
    """单文档转换会话：去重、限额、存档、VLM 理解。

    source_to_md_service 每次转换创建一个实例，随抽取过程逐图 add()。
    """

    def __init__(self, vault_dir: str, on_image: Optional[Callable[[int], None]] = None):
        self.vault_dir = vault_dir
        self.on_image = on_image
        # rel path -> {"desc": str}
        self.images: dict = {}
        self._seen_md5: set = set()
        self.count = 0
        self.max_images = int(settings.DOC_IMAGE_MAX_PER_DOC)

    def add(self, data: Optional[bytes], ext: str, bbox: Optional[list] = None) -> str:
        """存档并理解一张图。返回插入正文的占位符；被过滤/超限返回空串。"""
        if not data:
            return ""
        if bbox is not None and len(bbox) >= 4:
            w = float(bbox[2] - bbox[0])
            h = float(bbox[3] - bbox[1])
            if (w and w < MIN_BBOX_PT) or (h and h < MIN_BBOX_PT):
                return ""
        if len(data) < MIN_BYTES:
            return ""
        if self.max_images > 0 and self.count >= self.max_images:
            return ""
        md5 = hashlib.md5(data).hexdigest()
        if md5 in self._seen_md5:
            return ""
        ext = (ext or "png").lower().lstrip(".")
        if ext not in _IMG_EXTS:
            ext = "png"
        self._seen_md5.add(md5)
        self.count += 1
        rel = f"media/{md5}.{ext}"
        target = os.path.join(self.vault_dir, rel.replace("/", os.sep))
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            if not os.path.isfile(target):
                with open(target, "wb") as f:
                    f.write(data)
        except OSError as e:
            logger.warning(f"[doc_images] 图片存档失败 {rel}: {e}")
            return ""
        desc = ""
        if vlm_enabled() and len(data) <= VLM_MAX_BYTES:
            desc = describe_image_bytes(data, ext)
        self.images[rel] = {"desc": desc}
        if self.on_image:
            try:
                self.on_image(self.count)
            except Exception:
                pass
        return f"\n<!-- IMG:{rel} -->\n"


def _md_block(rel: str, desc: str) -> str:
    block = f"![图像]({rel})"
    if desc:
        quoted = "\n".join("> " + ln for ln in desc.splitlines())
        block += f"\n\n{quoted}"
    return block


def _raw_block(rel: str, desc: str) -> str:
    lines = [f"【图像 {rel}】"]
    if desc:
        lines.append(f"图像理解：{desc}")
    return "\n".join(lines)


def expand_placeholders(text: str, images: dict, for_raw: bool) -> str:
    """把 <!-- IMG:rel --> 占位符展开为图片块。

    for_raw=True：旁挂 .raw.txt（检索切块源），纯文本块；
    for_raw=False：笔记 md，markdown 图片 + 引用块描述。
    LLM 改写丢失的占位符统一追加文末「附图」段，保证图片不丢。"""
    if not images or not text:
        return text
    consumed = set()

    def _block(rel: str) -> str:
        info = images.get(rel)
        if info is None:
            return ""
        desc = (info.get("desc") or "").strip()
        return _raw_block(rel, desc) if for_raw else _md_block(rel, desc)

    def _sub(m: "re.Match") -> str:
        rel = m.group(1)
        if rel not in images:
            return m.group(0)
        b = _block(rel)
        if not b:
            return m.group(0)
        consumed.add(rel)
        return "\n" + b + "\n" if for_raw else "\n\n" + b + "\n\n"

    out = PLACEHOLDER_RE.sub(_sub, text)
    missing = [rel for rel in images if rel not in consumed]
    if missing:
        blocks = [_block(rel) for rel in missing]
        blocks = [b for b in blocks if b]
        if blocks:
            out = out.rstrip("\n") + "\n\n## 附图\n\n" + "\n\n".join(blocks) + "\n"
    return out
