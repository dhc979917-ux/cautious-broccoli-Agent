# -*- coding: utf-8 -*-
"""OCR Skill — 配置（引擎选择、模型端点、图片来源后端）。

两条路：
    rapid  本地离线引擎（RapidOCR + onnxruntime），随沙箱镜像预装，不依赖外网与配额
    vlm    平台限权 OCR 代理调用模型网关，可解析图表/表格/示意图

默认 auto：收到专用短期凭证就优先 vlm，失败自动回落 rapid。
沙箱不接触网关主密钥。
"""
import os

# ── 引擎 ──────────────────────────────────────────────────────────────────
DEFAULT_ENGINE = (os.environ.get("OCR_ENGINE") or "auto").strip().lower()
ENGINES = ("auto", "rapid", "vlm", "both")

# ── RapidOCR（离线）───────────────────────────────────────────────────────
# 首次构造引擎约 0.6 秒，进程内复用；识别语言默认中英混合
RAPID_LANG = (os.environ.get("OCR_RAPID_LANG") or "ch").strip()

# ── VLM（后端限权代理）────────────────────────────────────────────────────
VLM_BASE_URL = (
    os.environ.get("OCR_PROXY_URL")
    or ""
).rstrip("/")
VLM_MODEL = "gateway-ocr"
VLM_TIMEOUT = int(os.environ.get("OCR_VLM_TIMEOUT") or 120)
# 提示词：默认要求"照抄文字、保留表格结构"，避免模型自由发挥编内容
VLM_PROMPT = (
    os.environ.get("OCR_VLM_PROMPT")
    or "请把图片中的全部文字原样抽取出来，保持原有的行列/层级结构；"
       "表格用 Markdown 表格表示，不要翻译、不要改写、不要补充图片中没有的内容。"
       "若图片中没有文字，只回复 (无文字)。"
)


def vlm_api_key() -> str:
    """只读取 OCR 限权凭证，不接受通用网关密钥。"""
    return (os.environ.get("OCR_PROXY_TOKEN") or "").strip()


def vlm_available() -> bool:
    """VLM 路径是否可用（只判 Key，出网失败由调用处回落处理）。"""
    return bool(vlm_api_key() and VLM_BASE_URL)


# ── 图片获取 ──────────────────────────────────────────────────────────────
# 本地找不到文件时，按这个后端地址走 /file?path= 取（需自备文件后端服务）
FILE_BACKEND_URL = (
    os.environ.get("OCR_FILE_BACKEND")
    or ""
).rstrip("/")
FILE_ROUTE = (os.environ.get("OCR_FILE_ROUTE") or "/file").strip() or "/file"
FETCH_TIMEOUT = int(os.environ.get("OCR_FETCH_TIMEOUT") or 120)

# VLM 侧对图片尺寸/体积有限制，超过就先等比缩小；base64 后体积约放大 4/3
MAX_SIDE_DEFAULT = int(os.environ.get("OCR_MAX_SIDE") or 2000)
MAX_BYTES_FOR_VLM = int(os.environ.get("OCR_MAX_BYTES_FOR_VLM") or 8 * 1024 * 1024)

# 支持的图片扩展名（其余类型直接拒，避免把 PDF/Office 当图片喂进去）
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tif", ".tiff", ".webp"}
