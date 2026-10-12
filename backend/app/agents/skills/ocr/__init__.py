# -*- coding: utf-8 -*-
"""OCR Skill — 图片文字识别（本地 RapidOCR + 云端 VLM 双路，自动降级）。

平台当前是纯文本模型，Read 工具也拒读二进制图片，Agent"看不见"图。
本技能把图片转成文字，补上这一环：
    ocr_image(source, engine="auto", mode="text"|"chart")  识别单张
    ocr_many([...])                                         批量识别（一轮分析常出 8~12 张图）
    image_to_base64(source)                                 图片 -> data URI（报告内嵌用）
    fetch_backend_image(filepath)                           按后端 /file?path= 取图（NFS 路径免转义）
    read_image_info(source)                                 只看尺寸/格式，不识别

source 支持：沙箱本地路径、后端 NFS 路径（/nfs_fds/...）、UNC 路径、
http(s) URL、data URI、裸 base64、bytes —— 不需要调用方自己拼 URL 或转义反斜杠。
"""

try:
    from .scripts.ocr_image import (
        fetch_backend_image,
        image_to_base64,
        ocr_image,
        ocr_many,
        read_image_info,
    )
except ImportError:  # 直接以脚本方式运行（scripts 在 sys.path 上）
    from ocr_image import (
        fetch_backend_image,
        image_to_base64,
        ocr_image,
        ocr_many,
        read_image_info,
    )

__all__ = [
    "ocr_image",
    "ocr_many",
    "image_to_base64",
    "fetch_backend_image",
    "read_image_info",
]
