# -*- coding: utf-8 -*-
"""ocr 技能验证脚本（离线，不依赖 RapidOCR / 云端 VLM）。

用法：
    python test/_test_ocr_skill.py

覆盖：图片来源解析（本地路径 / data URI / 裸 base64 / 后端 NFS 路径 / UNC / URL）、
尺寸归一、双引擎分派与自动降级、对外辅助函数、CLI 输出。
真实引擎在沙箱镜像里验证（见 test/_test_ocr_sandbox.sh 或服务器端手动跑）。
"""
import base64
import io
import json
import os
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OCR_SCRIPTS = REPO_ROOT / "backend" / "app" / "agents" / "skills" / "ocr" / "scripts"
sys.path.insert(0, str(REPO_ROOT / "backend" / "app" / "agents" / "skills"))
sys.path.insert(0, str(OCR_SCRIPTS))

SCRATCH = REPO_ROOT / "test" / ".scratch_ocr"

_PASS = 0
_FAIL = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if ok:
        _PASS += 1
        print(f"  [PASS] {label}")
    else:
        _FAIL += 1
        print(f"  [FAIL] {label} {detail}")


def make_png(path: Path, size=(600, 300), text="站点 2200 Wire Sawing 1SWS21") -> bytes:
    """造一张带中英文的测试图。"""
    from PIL import Image, ImageDraw
    img = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(img)
    draw.text((20, 40), text, fill="black")
    draw.text((20, 140), "NG组专用机台占比 100.0%", fill="black")
    img.save(path, format="PNG")
    return path.read_bytes()


class FakeResp:
    def __init__(self, content: bytes, status_code: int = 200, text: str = ""):
        self.content = content
        self.status_code = status_code
        self.text = text or ""


def install_fake_session(loader, png_bytes: bytes):
    """把 _loader 的出网层换成假的，记录被请求的 URL。"""
    calls = []

    class FakeSession:
        def get(self, url, params=None, timeout=None, **kw):
            calls.append((url, params))
            if "nfs_fds" in url or (params and "nfs_fds" in str(params.get("path", ""))):
                return FakeResp(png_bytes)
            if url.startswith("http://example.com/ok"):
                return FakeResp(png_bytes)
            return FakeResp(b"", status_code=404, text="not found")

    loader._SESSION = FakeSession()
    return calls


def main() -> int:
    print("\n== ocr 技能离线检查 ==")
    shutil.rmtree(SCRATCH, ignore_errors=True)
    SCRATCH.mkdir(parents=True, exist_ok=True)

    png_path = SCRATCH / "sample.png"
    png_bytes = make_png(png_path)
    b64_text = base64.b64encode(png_bytes).decode()

    import _loader
    import ocr_image as oi

    calls = install_fake_session(_loader, png_bytes)

    # ── 来源解析 ──
    data, kind = _loader.load_bytes(str(png_path))
    check("来源: 本地路径", kind == "local" and data == png_bytes, kind)

    data, kind = _loader.load_bytes("data:image/png;base64," + b64_text)
    check("来源: data URI", kind == "data_uri" and data == png_bytes, kind)

    data, kind = _loader.load_bytes(b64_text)
    check("来源: 裸 base64", kind == "base64" and data == png_bytes, kind)

    data, kind = _loader.load_bytes(png_bytes)
    check("来源: bytes 直传", kind == "bytes" and data == png_bytes, kind)

    nfs = "/nfs_fds/R&D_data_analysis/coa/x/机台履历调查_SHA.png"
    try:
        data, kind = _loader.load_bytes(nfs)
        check("来源: 后端 NFS 路径走 /file?path=", kind == "backend_file" and data == png_bytes, kind)
    except _loader.ImageLoadError as e:
        check("来源: 后端 NFS 路径走 /file?path=", False, str(e)[:160])
    # 关键在于 & 和中文必须被编码进 ?path=，否则后端会把 R&D 截成两个参数
    nfs_call = next((u for u, _p in calls if "nfs_fds" in u), "")
    check("来源: NFS 路径经 URL 编码放进 ?path=",
          "?path=" in nfs_call and "R%26D_data_analysis" in nfs_call and "%2Fnfs_fds" in nfs_call,
          nfs_call[:200])

    unc = "\\\\10.20.1.251\\FDS_Analysis_Files\\a\\SHA.png"
    try:
        _loader.load_bytes(unc)
        check("来源: UNC 反斜杠路径不崩", True)
    except _loader.ImageLoadError:
        check("来源: UNC 反斜杠路径不崩", True)  # 假 session 不给数据也算通过（重点是没抛别的异常）

    data, kind = _loader.load_bytes("http://example.com/ok.png")
    check("来源: http URL", kind == "url" and data == png_bytes, kind)

    for bad in ("", "   ", "/definitely/not/here.png", "notafile"):
        try:
            _loader.load_bytes(bad)
            check(f"来源: 非法输入报错 {bad!r}", False)
        except _loader.ImageLoadError:
            check(f"来源: 非法输入报错 {bad!r}", True)

    try:
        _loader.load_bytes(str(SCRATCH))
        check("来源: 目录被拒", False)
    except _loader.ImageLoadError:
        check("来源: 目录被拒", True)

    # ── 尺寸归一 ──
    big = SCRATCH / "big.png"
    make_png(big, size=(4000, 2000))
    norm, info = _loader.normalize_for_model(big.read_bytes(), max_side=1000)
    check("归一: 超长边被等比缩小",
          info["resized"] and info["width"] == 1000 and info["height"] == 500, json.dumps(info))
    check("归一: 小图不动", _loader.normalize_for_model(png_bytes, max_side=2000)[1]["resized"] is False)

    # ── 引擎分派与降级（打桩，不依赖真实引擎）──
    real_rapid, real_vlm = oi._rapid.recognize, oi._vlm.recognize
    real_rapid_avail, real_vlm_avail = oi._rapid.is_available, oi._vlm.is_available
    try:
        oi._rapid.recognize = lambda data, detail="text": {
            "engine": "rapid", "text": "LOCAL-TEXT", "line_count": 1, "elapsed_seconds": 0.1,
            "blocks": [{"text": "LOCAL-TEXT", "confidence": 0.99, "box": [[0, 0], [1, 0], [1, 1], [0, 1]]}],
        }
        oi._vlm.recognize = lambda data, mode="text", **kw: {
            "engine": "vlm", "model": "qwen-vl-ocr", "mode": mode,
            "text": "VLM-TEXT", "line_count": 1, "elapsed_seconds": 0.2,
        }

        r = oi.ocr_image(str(png_path), engine="rapid")
        check("引擎: rapid 强制走本地", r["engine"] == "rapid" and r["text"] == "LOCAL-TEXT")

        r = oi.ocr_image(str(png_path), engine="vlm", mode="chart")
        check("引擎: vlm 强制走云端且带 mode", r["engine"] == "vlm" and r["text"] == "VLM-TEXT")

        r = oi.ocr_image(str(png_path), engine="both")
        check("引擎: both 两个都跑", set(r["engines"]) == {"rapid", "vlm"}, str(r.get("engines", {}).keys()))

        r = oi.ocr_image(str(png_path), engine="auto")
        check("引擎: auto 无凭据时回落本地", r["engine"] == "rapid", r["engine"])

        # 模拟配了凭据：auto 应优先 VLM
        os.environ["OCR_VLM_API_KEY"] = "fake-key"
        import _config
        _config.vlm_api_key = lambda: "fake-key"
        oi._vlm.is_available = lambda: (True, "")
        r = oi.ocr_image(str(png_path), engine="auto")
        check("引擎: auto 有凭据时优先 VLM", r["engine"] == "vlm", r["engine"])

        # 模拟 VLM 挂掉：auto 应回落本地并留下 warning
        def boom(*a, **kw):
            raise RuntimeError("VLM 返回 HTTP 429: quota exceeded")
        oi._vlm.recognize = boom
        r = oi.ocr_image(str(png_path), engine="auto")
        check("引擎: VLM 失败自动回落本地", r["engine"] == "rapid" and r["text"] == "LOCAL-TEXT")
        check("引擎: 回落留痕于 warnings", any("回落" in w for w in r["warnings"]), str(r["warnings"]))
        try:
            oi.ocr_image(str(png_path), engine="vlm")
            check("引擎: 强制 vlm 失败不静默降级", False)
        except RuntimeError:
            check("引擎: 强制 vlm 失败不静默降级", True)

        # ── 输出选项 ──
        oi._vlm.recognize = lambda data, mode="text", **kw: {"engine": "vlm", "text": "VLM-TEXT", "line_count": 1}
        r = oi.ocr_image(str(png_path), engine="rapid", detail="layout")
        check("输出: layout 附带 blocks", bool(r.get("blocks")), str(r.get("blocks"))[:120])
        r = oi.ocr_image(str(png_path), engine="rapid", with_base64=True)
        check("输出: with_base64 给 data URI", r.get("base64", "").startswith("data:image/png;base64,"))
        out_txt = SCRATCH / "out.txt"
        r = oi.ocr_image(str(png_path), engine="rapid", save_text_to=str(out_txt))
        check("输出: 文本另存", out_txt.exists() and out_txt.read_text(encoding="utf-8") == "LOCAL-TEXT")

        # ── 批量 ──
        batch = oi.ocr_many([str(png_path), "/definitely/missing.png"], engine="rapid")
        check("批量: 成功失败分别计数", batch["ok"] == 1 and batch["failed_count"] == 1, json.dumps(
            {k: batch[k] for k in ("total", "ok", "failed_count")}))
        check("批量: 合并文本可用", "LOCAL-TEXT" in batch["text"])

        # ── 对外辅助 ──
        check("辅助: image_to_base64", oi.image_to_base64(str(png_path)).startswith("data:image/png;base64,"))
        check("辅助: fetch_backend_image", oi.fetch_backend_image(nfs) == png_bytes)
        info = oi.read_image_info(str(png_path))
        check("辅助: read_image_info", info["width"] == 600 and info["format"] == "PNG", json.dumps(info))

        # ── CLI ──
        rc = oi.main([str(png_path), "--engine", "rapid"])
        check("CLI: 单图退出码 0", rc == 0, str(rc))
        rc = oi.main([str(png_path), "--engine", "rapid", "--json"])
        check("CLI: --json 退出码 0", rc == 0, str(rc))
        rc = oi.main(["/definitely/missing.png", "--engine", "rapid"])
        check("CLI: 取不到图退出码非 0", rc == 1, str(rc))
    finally:
        oi._rapid.recognize, oi._vlm.recognize = real_rapid, real_vlm
        oi._rapid.is_available, oi._vlm.is_available = real_rapid_avail, real_vlm_avail
        os.environ.pop("OCR_VLM_API_KEY", None)

    shutil.rmtree(SCRATCH, ignore_errors=True)
    print(f"\n结果: {_PASS} 通过, {_FAIL} 失败")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
