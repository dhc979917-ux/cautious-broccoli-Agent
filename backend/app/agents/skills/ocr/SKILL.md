---
name: ocr
description: >
  图片文字识别技能（OCR）。把图片读成文字，供纯文本模型使用：本地离线引擎
  RapidOCR（随沙箱镜像预装，不联网不耗配额）+ 云端视觉大模型 qwen-vl-ocr
  （能读懂行军图/Q-time柱状图/链路树形图/缺陷MAP 等图表语义），配了凭据优先
  云端、失败自动回落本地。输入支持沙箱本地路径、后端 NFS 路径(/nfs_fds/...)、
  UNC 路径(\\\\host\\share\\...)、http(s) URL、data URI、裸 base64，调用方不必自己
  拼 URL 或转义反斜杠。关键词: OCR, 识别图片文字, 图片内容, 读图, 图表识别,
  image to text, 截图文字, 表格识别, base64 图片。
metadata:
  version: 1.0.0
compatibility: >
  需要 Pillow（沙箱镜像已预装）。本地引擎需要 rapidocr-onnxruntime +
  opencv-python-headless（已烤进 docker/Dockerfile.skill）。云端引擎需要
  由平台下发短期、限权的 OCR_PROXY_TOKEN；缺失时自动只用本地引擎。
---

# OCR — 图片文字识别

## 什么时候用

平台的 `Read` 工具会拒绝二进制图片，模型本身也收不到图像，所以**凡是"需要知道
图里写了什么/画了什么"，都走本技能**，不要尝试用 `Read` 打开图片，也不要臆造图片内容。

典型场景：
- 用户上传了截图/照片/扫描件，问"帮我看看这张图"
- 其他技能（如 `defect-trace-analysis`）产出了图表，需要读图里的站点号、机台号、
  参数名、数值趋势来做分析
- 需要把图片里的表格转成文字/Markdown 继续处理
- 需要把图片转成 base64 data URI 内嵌进 HTML 报告

## 怎么用

### 命令行（推荐，Bash 直接调）

```bash
# 最简：识别本地图片，输出纯文字
python3 /workspace/skills/ocr/scripts/ocr_image.py /workspace/_attachments/x.png

# 图表语义理解（行军图/柱状图/树形图/MAP 用这个档位，比"照抄文字"有用得多）
python3 /workspace/skills/ocr/scripts/ocr_image.py "<图片路径>" --mode chart

# 后端 NFS 路径直接传，不用自己拼 URL、不用转义 & 和反斜杠
python3 /workspace/skills/ocr/scripts/ocr_image.py "/nfs_fds/R&D_data_analysis/.../机台履历调查_SHA.png"

# 批量：一次传多张，输出会分段
python3 /workspace/skills/ocr/scripts/ocr_image.py a.png b.png c.png --mode chart

# 带每行置信度与坐标框（核对识别质量时用）
python3 /workspace/skills/ocr/scripts/ocr_image.py x.png --detail layout --json

# 只用本地离线引擎（不出网、不耗配额）
python3 /workspace/skills/ocr/scripts/ocr_image.py x.png --engine rapid

# 识别结果另存文件
python3 /workspace/skills/ocr/scripts/ocr_image.py x.png --save-text /workspace/outputs/x.txt
```

### 作为 Python 库调用

```python
import sys; sys.path.insert(0, "/workspace/skills/ocr/scripts")
from ocr_image import ocr_image, ocr_many, image_to_base64, fetch_backend_image

r = ocr_image("/workspace/_attachments/x.png", mode="chart")
print(r["text"], r["engine"], r["elapsed_seconds"])

# 批量（一轮不良率分析常出 8~12 张图）
batch = ocr_many([p1, p2, p3], mode="chart")
print(batch["ok"], batch["failed_count"], batch["text"])

# 图片 -> data URI（内嵌进自包含 HTML 报告）
uri = image_to_base64(p1)

# 只要二进制（NFS 路径的反斜杠/&/中文都由技能内部处理）
data = fetch_backend_image(r"\\10.20.1.251\FDS_Analysis_Files\a\SHA.png")
```

## 参数

| 参数 | 取值 | 默认 | 说明 |
|------|------|------|------|
| `source` | 路径/URL/data URI/base64/bytes | 必填 | 图片来源，形态自动识别 |
| `engine` | `auto` `rapid` `vlm` `both` | `auto` | auto=有 VLM 凭据优先 VLM，失败回落本地 |
| `mode` | `text` `chart` | `text` | VLM 提示词档位；chart=图表语义理解 |
| `detail` | `text` `layout` | `text` | layout 附带每行置信度与四点框 |
| `max_side` | 像素 | 2000 | 送模型前等比缩小的最长边，0=不缩 |
| `with_base64` | bool | False | 返回里带图片 data URI（很大，按需开） |
| `save_text_to` | 路径 | 无 | 识别文本另存 |

## 返回

```json
{
  "source": "/workspace/_attachments/x.png",
  "source_kind": "local",
  "engine": "vlm",
  "engine_requested": "auto",
  "mode": "chart",
  "text": "识别出的文字…",
  "line_count": 12,
  "image": {"width": 1052, "height": 673, "format": "PNG", "size_bytes": 158296, "resized": false},
  "elapsed_seconds": 2.31,
  "warnings": []
}
```

`warnings` 非空说明发生了降级（例如 VLM 无凭据/出网失败，已回落本地引擎），
此时 `engine` 字段是实际生效的那个。

## 环境变量

| 变量 | 默认 | 说明 |
|------|------|------|
| `OCR_ENGINE` | `auto` | 默认引擎 |
| `OCR_PROXY_TOKEN` | 空 | 由平台向沙箱签发的 OCR 专用短期凭证，不能代替网关主密钥 |
| `OCR_PROXY_URL` | 空 | 平台 OCR 代理地址；留空只使用本地引擎 |
| `OCR_VLM_MODEL` | `qwen-vl-ocr` | 视觉模型名 |
| `OCR_VLM_PROMPT` | 内置 | 覆盖 `mode=text` 的默认提示词 |
| `OCR_FILE_BACKEND` | 空 | 远端文件后端的取图地址（按需自备） |
| `OCR_FILE_ROUTE` | `/file` | 取图路由 |
| `OCR_MAX_SIDE` | 2000 | 默认缩放最长边 |
| `OCR_RAPID_LANG` | `ch` | 本地引擎语言 |

沙箱默认**不注入**任何密钥。要走 VLM 路径，需平台侧把
不要向沙箱透传网关主密钥；云端识别由 52 平台后端的 OCR 代理完成。
没配也能用，只是只剩本地引擎的"照抄文字"能力。

## 注意事项

- **激活技能时请连带激活 `file_ops` 工具组**：`reset_tools` 是替换而非追加，
  只激活本技能会把 `Read/Glob/Grep` 挤掉，随后调 `Read` 会报 ToolNotFoundError。
  例：`{"skills_ocr": true, "file_ops": true}`（组名以工具列表里实际显示的为准）。
- 本地引擎只能"抄字"，对散点图/热力图这类**没有文字标注的图形**给不出有效信息；
  这种图请用 `--mode chart` 走 VLM，或直接把图片随报告交付给用户看。
- 本地引擎首次调用约 0.6 秒加载模型，单张约 1 秒；VLM 单张约 2~5 秒并消耗配额。
  批量刷图优先本地引擎，只有需要图形语义时才用 VLM。
- 依赖必须**烤进沙箱镜像**（docker/Dockerfile.skill）。运行时 `pip install`
  实测要 4 分钟以上，会把工具调用超时耗光。
- `rapidocr-onnxruntime` 会拉 `opencv-python`，slim 镜像缺 `libGL.so.1` 会 import 失败；
  镜像里必须换装 `opencv-python-headless`。
