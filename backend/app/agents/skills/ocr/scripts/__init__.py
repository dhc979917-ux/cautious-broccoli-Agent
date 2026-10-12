# -*- coding: utf-8 -*-
"""OCR Skill — scripts 子包。

分层（下层不反向依赖上层）：
    _config    引擎选择、VLM 端点/模型、文件后端地址、尺寸阈值
    _loader    图片来源解析（本地/NFS/UNC/URL/data URI/base64）与尺寸归一
    _rapid     本地离线引擎（RapidOCR + onnxruntime，随沙箱镜像预装）
    _vlm       云端视觉大模型引擎（DashScope 兼容模式，读得懂图表语义）
    ocr_image  对外入口与 CLI（双引擎 + 自动降级）
"""
