# -*- coding: utf-8 -*-
"""OCR Skill — 图片来源解析（需求 2/3 的接入点）。

一个 source 参数吃下 Agent 可能拿到的所有形态，避免模型自己去拼 URL、
转义反斜杠（实测这是它最容易出错的地方）：
    - 沙箱内本地路径      /workspace/_attachments/x.png
    - 后端侧 NFS 路径     /nfs_fds/R&D_data_analysis/.../图.png   -> GET /file?path=
    - NFS UNC 路径        \\\\10.20.1.251\\FDS_Analysis_Files\\...\\SHA.png
    - http(s) URL         http://host/file?path=...
    - data URI            data:image/png;base64,iVBORw0KGgo...
    - 裸 base64 字符串    iVBORw0KGgo...

同时负责尺寸归一：VLM 对像素和体积都有限制，超大图先等比缩小再送。
"""
import base64
import binascii
import io
import os
import re
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode

import requests

try:
    from ._config import (
        FETCH_TIMEOUT, FILE_BACKEND_URL, FILE_ROUTE, IMAGE_SUFFIXES, MAX_BYTES_FOR_VLM,
        MAX_SIDE_DEFAULT,
    )
except ImportError:  # 直接以脚本方式运行
    from _config import (
        FETCH_TIMEOUT, FILE_BACKEND_URL, FILE_ROUTE, IMAGE_SUFFIXES, MAX_BYTES_FOR_VLM,
        MAX_SIDE_DEFAULT,
    )

_DATA_URI_RE = re.compile(r"^data:([\w.+-]+/[\w.+-]+)?;base64,(.*)$", re.DOTALL)
_B64_RE = re.compile(r"^[A-Za-z0-9+/_\-=\s]{64,}$")

# 取后端文件同样走内网直连，别让环境代理把内网地址劫走
_SESSION: Optional[requests.Session] = None


def _session() -> requests.Session:
    global _SESSION
    if _SESSION is None:
        s = requests.Session()
        s.trust_env = False
        _SESSION = s
    return _SESSION


class ImageLoadError(Exception):
    """图片来源无法解析或不是图片。"""


def _from_base64(text: str) -> bytes:
    """解 base64（容忍 data URI 前缀、URL-safe 字母表与换行）。"""
    m = _DATA_URI_RE.match(text.strip())
    payload = m.group(2) if m else text
    cleaned = re.sub(r"\s+", "", payload).replace("-", "+").replace("_", "/")
    cleaned += "=" * (-len(cleaned) % 4)
    try:
        return base64.b64decode(cleaned, validate=False)
    except (binascii.Error, ValueError) as e:
        raise ImageLoadError(f"base64 解码失败: {e}") from e


def _fetch_backend_file(filepath: str, timeout: int = FETCH_TIMEOUT) -> bytes:
    """按后端 /file?path= 路由取文件（defect-trace 的 NFS 图片就是这个形态）。"""
    url = f"{FILE_BACKEND_URL}{FILE_ROUTE}?{urlencode({'path': filepath})}"
    try:
        resp = _session().get(url, timeout=timeout)
    except requests.RequestException as e:
        raise ImageLoadError(f"取后端文件失败 {filepath}: {type(e).__name__} {e}") from e
    if resp.status_code != 200:
        raise ImageLoadError(
            f"取后端文件失败 (HTTP {resp.status_code}) path={filepath}: {resp.text[:200]}"
        )
    if not resp.content:
        raise ImageLoadError(f"后端返回空内容: {filepath}")
    return resp.content


def _local_file(path: Path) -> Optional[Path]:
    """本地存在且是文件才返回它，其余情况一律返回 None。

    不能直接用 Path.exists()：它对"路径不可探测"会抛异常而不是返回 False
    （Windows 上访问不可达的 UNC 路径 \\\\host\\share 会抛 PermissionError，
    pathlib 只吞 ENOENT/ENOTDIR 这类错误）。这里把所有 OSError 都当"本地没有"，
    让调用方继续走后端取图那条路。
    """
    try:
        return path if path.is_file() else None
    except OSError:
        return None


def load_bytes(source: Any, timeout: int = FETCH_TIMEOUT) -> tuple[bytes, str]:
    """把任意形态的 source 解析成图片二进制。

    Returns:
        (bytes, kind)：kind 为 local/url/backend_file/data_uri/base64，便于日志与排错

    Raises:
        ImageLoadError: 解析不到、取不到、或内容为空
    """
    if isinstance(source, (bytes, bytearray)):
        return bytes(source), "bytes"

    text = str(source or "").strip()
    if not text:
        raise ImageLoadError("source 为空")

    # 1) data URI / 裸 base64（长度门槛避免把普通路径误判成 base64）
    if text.startswith("data:"):
        return _from_base64(text), "data_uri"

    # 2) http(s) URL
    if text.startswith(("http://", "https://")):
        try:
            resp = _session().get(text, timeout=timeout)
        except requests.RequestException as e:
            raise ImageLoadError(f"下载失败 {text}: {type(e).__name__} {e}") from e
        if resp.status_code != 200:
            raise ImageLoadError(f"下载失败 (HTTP {resp.status_code}) {text}")
        return resp.content, "url"

    # 3) 本地路径（沙箱内）
    candidate = Path(text)
    try:
        is_dir = candidate.is_dir()
    except OSError:
        is_dir = False
    if is_dir:
        raise ImageLoadError(f"source 是目录不是图片: {text}")
    local = _local_file(candidate)
    if local is not None:
        try:
            data = local.read_bytes()
        except OSError as e:
            raise ImageLoadError(f"读取本地文件失败 {text}: {e}") from e
        if not data:
            raise ImageLoadError(f"文件为空: {text}")
        return data, "local"

    # 4) 本地不存在 -> 当后端侧路径（/nfs_fds/... 或 UNC \\host\share\...）去取
    if text.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:[\\/]", text):
        return _fetch_backend_file(text, timeout=timeout), "backend_file"

    # 5) 最后试裸 base64
    if _B64_RE.match(text):
        return _from_base64(text), "base64"

    raise ImageLoadError(
        f"无法识别的图片来源: {text[:120]}（本地不存在，也不像 URL / base64 / 后端路径）"
    )


def open_image(data: bytes) -> Any:
    """用 PIL 打开图片，失败即抛 ImageLoadError（顺便挡住"其实是 PDF/Office"的误用）。"""
    try:
        from PIL import Image
    except ImportError as e:  # 沙箱镜像已预装 Pillow，缺了说明环境不对
        raise ImageLoadError(f"Pillow 不可用: {e}") from e
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
        return img
    except Exception as e:
        raise ImageLoadError(f"不是可解析的图片: {type(e).__name__} {e}") from e


def normalize_for_model(
    data: bytes,
    max_side: int = MAX_SIDE_DEFAULT,
    max_bytes: int = MAX_BYTES_FOR_VLM,
) -> tuple[bytes, dict[str, Any]]:
    """尺寸/体积归一：等比缩到 max_side，必要时转 RGB JPEG 压到 max_bytes 以内。

    Returns:
        (bytes, info)：info 含 width/height/format/resized/mode
    """
    img = open_image(data)
    info: dict[str, Any] = {
        "width": img.width, "height": img.height,
        "format": (img.format or "").upper(), "mode": img.mode,
        "size_bytes": len(data), "resized": False,
    }

    out_data, out_img = data, img
    longest = max(img.width, img.height)
    if max_side and longest > max_side:
        from PIL import Image as _PILImage
        scale = max_side / float(longest)
        new_size = (max(1, int(img.width * scale)), max(1, int(img.height * scale)))
        out_img = img.resize(new_size, _PILImage.Resampling.LANCZOS)
        buf = io.BytesIO()
        fmt = "PNG" if (out_img.mode in ("RGBA", "LA", "P") or info["format"] == "PNG") else "JPEG"
        if fmt == "JPEG" and out_img.mode not in ("RGB", "L"):
            out_img = out_img.convert("RGB")
        out_img.save(buf, format=fmt, quality=90)
        out_data = buf.getvalue()
        info.update({"width": out_img.width, "height": out_img.height, "resized": True})

    # 体积仍超限：转 JPEG 逐档降质量（图表类图片这样做损失可接受）
    if max_bytes and len(out_data) > max_bytes:
        if out_img.mode not in ("RGB", "L"):
            out_img = out_img.convert("RGB")
        for quality in (85, 70, 55, 40):
            buf = io.BytesIO()
            out_img.save(buf, format="JPEG", quality=quality)
            out_data = buf.getvalue()
            if len(out_data) <= max_bytes:
                break
        info["format"] = "JPEG"

    info["size_bytes"] = len(out_data)
    return out_data, info


def to_data_uri(data: bytes, filename: str = "") -> str:
    """二进制 -> data URI（内嵌 HTML 或直接喂 VLM 用）。"""
    suffix = Path(str(filename)).suffix.lower()
    mime = {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".gif": "image/gif", ".bmp": "image/bmp", ".webp": "image/webp",
        ".tif": "image/tiff", ".tiff": "image/tiff",
    }.get(suffix, "image/png")
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def describe_source(source: Any) -> str:
    """给日志/返回体用的简短来源描述（不打印超长 base64）。"""
    text = source if isinstance(source, str) else f"<{type(source).__name__}>"
    return text if len(text) <= 160 else text[:80] + f"...({len(text)} chars)"


def is_supported_suffix(filename: str) -> bool:
    """扩展名是否属于图片（后缀缺失不拦，交给 open_image 判真伪）。"""
    suffix = Path(str(filename)).suffix.lower()
    return (not suffix) or suffix in IMAGE_SUFFIXES


def ensure_local_copy(source: Any, dest_dir: Any, timeout: int = FETCH_TIMEOUT) -> str:
    """把远端来源落地成本地文件并返回路径（本地已存在则原样返回）。

    RapidOCR 对"路径"和"二进制"都支持，但把图落地一份便于复现与排错。
    """
    text = str(source or "").strip()
    candidate = Path(text)
    if _local_file(candidate) is not None:
        return str(candidate.resolve())

    data, kind = load_bytes(source, timeout=timeout)
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    name = candidate.name if candidate.name else f"ocr_{abs(hash(text)) % 10**8}.png"
    target = dest / name
    target.write_bytes(data)
    return str(target.resolve())
