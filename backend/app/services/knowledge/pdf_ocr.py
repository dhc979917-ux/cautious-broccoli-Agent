"""OCR 前置层：扫描版 PDF 的文字抢救（检索芯 v2.5）。

问题（2026-10-16 验收实测）：DDSG 等日系设备手册是扫描版 PDF——
无文字层，PyMuPDF 整册只能抠出 29~400 字符（目录残片），正文全是
图片像素。source_to_md 的提取器对这类文件等于空转。

方案：上传文档时检测"文字层稀疏"的 PDF → 渲染成图 → 本地 RapidOCR
（onnxruntime CPU 推理，离线免费，与 ocr 技能同源）逐页识别 →
拼接成文本走现有管道。PyMuPDF 已能读出足够文字的 PDF 不走 OCR，
避免无谓开销。

设计约束：
- 可选依赖：rapidocr 未安装时 PDF_OCR_ENABLED 自动视为关闭，
  行为与改造前一致（提取结果稀疏就直接稀疏着入库）；
- 置信度过滤：低于阈值（默认 0.55）的识别块丢弃，防噪；
- 每页结果带 [第N页] 标记，保留可溯源的页码锚点；
- DPI 200：实测识别率与速度的平衡点（每页 ~1.5s CPU）。
"""

import logging
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

# 文字层判定：PDF 总字符数低于 每页阈值×页数 视为扫描版
CHARS_PER_PAGE_THRESHOLD = 50
OCR_DPI = 200
MIN_CONFIDENCE = 0.55
MAX_PAGES = 300  # 超大册子截断，防单文件 OCR 跑不完


def ocr_available() -> bool:
    try:
        from rapidocr_onnxruntime import RapidOCR  # noqa: F401
        return True
    except Exception:
        return False


def is_scanned_pdf(pdf_path: str) -> Tuple[bool, int, int]:
    """检测 PDF 是否为扫描版。返回 (是扫描版, 页数, 现有文字字符数)。"""
    import fitz
    doc = fitz.open(pdf_path)
    try:
        pages = len(doc)
        chars = sum(len(pg.get_text().strip()) for pg in doc)
        return (pages > 0 and chars < CHARS_PER_PAGE_THRESHOLD * pages), pages, chars
    finally:
        doc.close()


def extract_pdf_with_ocr(pdf_path: str,
                         on_progress=None,
                         should_cancel=None) -> Optional[str]:
    """渲染每页 → RapidOCR 识别 → 拼接带页码标记的文本。

    返回 None 表示不可用（依赖缺失/空结果），调用方回退原提取器。
    """
    try:
        import fitz
        from rapidocr_onnxruntime import RapidOCR
    except Exception as e:
        logger.warning(f"[ocr前置] 依赖不可用，回退默认提取: {e}")
        return None

    ocr = RapidOCR()
    doc = fitz.open(pdf_path)
    try:
        pages = min(len(doc), MAX_PAGES)
        parts: List[str] = []
        for i in range(pages):
            if should_cancel and should_cancel():
                raise KeyboardInterrupt()
            pix = doc[i].get_pixmap(dpi=OCR_DPI)
            img_path = f"{pdf_path}.ocr_{i}.png"
            pix.save(img_path)
            try:
                result, _ = ocr(img_path)
            finally:
                try:
                    import os
                    os.remove(img_path)
                except OSError:
                    pass
            if result:
                lines = [r[1] for r in result
                         if len(r) >= 3 and float(r[2]) >= MIN_CONFIDENCE]
                if lines:
                    parts.append(f"[第{i + 1}页] " + "\n".join(lines))
            if on_progress and pages > 1:
                try:
                    on_progress(int((i + 1) / pages * 100), f"OCR 第 {i + 1}/{pages} 页")
                except Exception:
                    pass
        return "\n\n".join(parts) if parts else None
    finally:
        doc.close()
