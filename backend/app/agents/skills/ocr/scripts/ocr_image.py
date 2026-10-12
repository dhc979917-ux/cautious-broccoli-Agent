# -*- coding: utf-8 -*-
"""OCR Skill — 对外入口：把图片读成文字（需求 2/3 的落地实现）。

双引擎 + 自动降级：
    auto（默认）  配了 VLM 凭据就先用 VLM（能读懂图表语义），失败自动回落本地
                  RapidOCR；没配凭据直接用本地引擎，全程离线可用
    rapid         只用本地离线引擎
    vlm           只用云端视觉大模型
    both          两个都跑，各自返回（对比/兜底用）

典型用法（Agent 在沙箱里）：
    python3 ocr_image.py /workspace/_attachments/x.png
    python3 ocr_image.py "/nfs_fds/R&D_data_analysis/.../机台履历调查_SHA.png" --mode chart

作为库调用：
    from ocr_image import ocr_image, ocr_many, image_to_base64
    ocr_image(source, engine="auto", mode="chart")["text"]
"""
import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Optional

try:
    from . import _rapid, _vlm
    from ._config import DEFAULT_ENGINE, MAX_SIDE_DEFAULT
    from ._loader import (
        ImageLoadError, describe_source, load_bytes, normalize_for_model, open_image, to_data_uri,
    )
except ImportError:  # 直接以脚本方式运行
    import _rapid
    import _vlm
    from _config import DEFAULT_ENGINE, MAX_SIDE_DEFAULT
    from _loader import (
        ImageLoadError, describe_source, load_bytes, normalize_for_model, open_image, to_data_uri,
    )


def _run_engine(
    name: str,
    data: bytes,
    mode: str,
    detail: str,
    prompt: Optional[str],
    model: Optional[str],
    timeout: int,
    filename: str,
) -> dict[str, Any]:
    """跑单个引擎，统一异常为 RuntimeError（调用方据此决定是否降级）。"""
    if name == "rapid":
        return _rapid.recognize(data, detail=detail)
    if name == "vlm":
        return _vlm.recognize(data, mode=mode, prompt=prompt, model=model,
                              timeout=timeout, filename=filename)
    raise RuntimeError(f"未知引擎: {name}")


def ocr_image(
    source: Any,
    engine: str = DEFAULT_ENGINE,
    mode: str = "text",
    detail: str = "text",
    max_side: int = MAX_SIDE_DEFAULT,
    prompt: Optional[str] = None,
    model: Optional[str] = None,
    timeout: int = 120,
    with_base64: bool = False,
    save_text_to: Optional[str] = None,
) -> dict[str, Any]:
    """识别一张图片。

    Args:
        source: 本地路径 / 后端 NFS 路径 / UNC 路径 / http(s) URL / data URI / base64 / bytes
        engine: auto | rapid | vlm | both
        mode: VLM 提示词档位，text=照抄文字，chart=图表语义理解（对行军图/柱状图/树形图更有效）
        detail: rapid 引擎输出粒度，text=整段文字，layout=附带每行置信度与框坐标
        max_side: 送模型前等比缩小的最长边像素，0=不缩
        prompt / model / timeout: VLM 自定义提示词、模型名、超时
        with_base64: 是否在返回里带上图片 data URI（需求 3；很大，默认关）
        save_text_to: 识别文本另存路径

    Returns:
        dict: {"source","engine","text","line_count","image","warnings",
               "elapsed_seconds","blocks"?,"vlm"?,"rapid"?,"base64"?}

    Raises:
        ImageLoadError: 图片取不到或不是图片
        RuntimeError: 指定引擎不可用且无降级路径
    """
    t0 = time.time()
    engine = (engine or "auto").strip().lower()
    if engine not in ("auto", "rapid", "vlm", "both"):
        raise ValueError(f"engine 只能是 auto/rapid/vlm/both，收到: {engine}")

    raw, kind = load_bytes(source, timeout=timeout)
    data, image_info = normalize_for_model(raw, max_side=max_side)
    image_info["source_kind"] = kind
    warnings: list[str] = []
    filename = Path(str(source)).name if isinstance(source, str) else ""

    results: dict[str, Any] = {}

    # 引擎尝试顺序：auto 按"有凭据就先云端"决定，both 两个都跑，指定则只跑一个
    if engine == "auto":
        order = (["vlm"] if _vlm.is_available()[0] else []) + ["rapid"]
    elif engine == "both":
        order = ["vlm", "rapid"]
    else:
        order = [engine]
    # 只有一个候选时失败必须抛；有多个候选时前序失败降级并留痕
    may_fallback = engine in ("auto", "both")

    for name in order:
        try:
            results[name] = _run_engine(name, data, mode, detail, prompt, model, timeout, filename)
        except RuntimeError as e:
            if not may_fallback:
                raise
            label = "云端 VLM" if name == "vlm" else "本地引擎"
            warnings.append(f"{label}不可用，已回落: {e}")
        if engine == "auto" and results:
            break  # auto 只需第一个成功的引擎，不浪费配额与时间

    # 主结果：按尝试顺序取第一个成功的（auto/both 下即优先 VLM）
    primary: dict[str, Any] = {}
    for name in order:
        if name in results:
            primary = results[name]
            break

    if not primary:
        raise RuntimeError("没有可用引擎完成识别: " + "; ".join(warnings))

    text = primary.get("text", "")
    if save_text_to:
        target = Path(save_text_to)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    out: dict[str, Any] = {
        "source": describe_source(source),
        "source_kind": kind,
        "engine": primary.get("engine", engine),
        "engine_requested": engine,
        "mode": mode,
        "text": text,
        "line_count": primary.get("line_count", len([x for x in text.splitlines() if x.strip()])),
        "image": image_info,
        "elapsed_seconds": round(time.time() - t0, 2),
        "warnings": warnings,
    }
    if detail == "layout" and "rapid" in results:
        out["blocks"] = results["rapid"].get("blocks", [])
    if engine == "both":
        out["engines"] = results
    if with_base64:
        out["base64"] = to_data_uri(data, filename)
    if save_text_to:
        out["saved_to"] = str(Path(save_text_to).resolve())
    return out


def ocr_many(
    sources: Iterable[Any],
    stop_on_error: bool = False,
    **kwargs: Any,
) -> dict[str, Any]:
    """批量识别（defect-trace 一轮就产 8~12 张图，逐张调太啰嗦）。

    Returns:
        dict: {"total","ok","failed","results":[{...,"source"}],"text": 合并文本}
    """
    results: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for source in sources or []:
        try:
            results.append(ocr_image(source, **kwargs))
        except (ImageLoadError, RuntimeError, ValueError) as e:
            failed.append({"source": describe_source(source), "error": f"{type(e).__name__}: {e}"})
            if stop_on_error:
                break
    return {
        "total": len(results) + len(failed),
        "ok": len(results),
        "failed_count": len(failed),
        "results": results,
        "failed": failed,
        "text": "\n\n".join(
            f"### {r['source']}\n{r['text']}" for r in results
        ),
    }


def image_to_base64(source: Any, max_side: int = 0, timeout: int = 120) -> str:
    """任意来源图片 -> data URI（需求 3 的服务端方法，供报告内嵌/直接喂模型）。"""
    raw, _kind = load_bytes(source, timeout=timeout)
    data = raw if not max_side else normalize_for_model(raw, max_side=max_side)[0]
    filename = Path(str(source)).name if isinstance(source, str) else ""
    return to_data_uri(data, filename)


def fetch_backend_image(filepath: str, timeout: int = 120) -> bytes:
    """按后端 /file?path= 取图片二进制（NFS 路径的反斜杠/&/中文都由这一层处理）。"""
    data, _kind = load_bytes(filepath, timeout=timeout)
    return data


def read_image_info(source: Any, timeout: int = 120) -> dict[str, Any]:
    """只看图片元信息（尺寸/格式/体积），不做识别 —— 快速自检用。"""
    raw, kind = load_bytes(source, timeout=timeout)
    img = open_image(raw)
    return {
        "source": describe_source(source), "source_kind": kind,
        "width": img.width, "height": img.height,
        "format": (img.format or "").upper(), "mode": img.mode,
        "size_bytes": len(raw),
    }


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="把图片读成文字（本地 RapidOCR + 云端 VLM 双路，自动降级）",
    )
    p.add_argument("sources", nargs="+", help="图片：本地路径/后端NFS路径/URL/data URI/base64")
    p.add_argument("--engine", default=DEFAULT_ENGINE, choices=["auto", "rapid", "vlm", "both"],
                   help="引擎选择，默认 auto（有 VLM 凭据优先 VLM，失败回落本地）")
    p.add_argument("--mode", default="text", choices=["text", "chart"],
                   help="VLM 提示词档位：text=照抄文字，chart=图表语义理解")
    p.add_argument("--detail", default="text", choices=["text", "layout"],
                   help="本地引擎输出粒度：layout 附带每行置信度与框坐标")
    p.add_argument("--max-side", type=int, default=MAX_SIDE_DEFAULT, help="送模型前最长边像素，0=不缩")
    p.add_argument("--model", default=None, help="VLM 模型名（默认取 OCR_VLM_MODEL/qwen-vl-ocr）")
    p.add_argument("--prompt", default=None, help="自定义 VLM 提示词")
    p.add_argument("--timeout", type=int, default=120, help="取图与 VLM 请求超时秒数")
    p.add_argument("--with-base64", action="store_true", help="返回里附带图片 data URI（很大）")
    p.add_argument("--save-text", default=None, help="识别文本另存到该文件（多张时自动加序号）")
    p.add_argument("--json", action="store_true", help="输出完整 JSON 而不是纯文本")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    kwargs = dict(
        engine=args.engine, mode=args.mode, detail=args.detail, max_side=args.max_side,
        prompt=args.prompt, model=args.model, timeout=args.timeout, with_base64=args.with_base64,
    )

    items = args.sources if len(args.sources) > 1 else [args.sources[0]]
    collected: list[dict[str, Any]] = []
    exit_code = 0

    for idx, source in enumerate(items, 1):
        save_to = None
        if args.save_text:
            path = Path(args.save_text)
            save_to = str(path if len(items) == 1
                          else path.with_name(f"{path.stem}_{idx}{path.suffix}"))
        try:
            result = ocr_image(source, save_text_to=save_to, **kwargs)
        except (ImageLoadError, RuntimeError, ValueError) as e:
            exit_code = 1
            collected.append({"source": describe_source(source), "error": f"{type(e).__name__}: {e}"})
            continue
        collected.append(result)

    if args.json:
        payload = collected[0] if len(collected) == 1 else {"total": len(collected), "results": collected}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return exit_code

    for result in collected:
        if result.get("error"):
            print(f"[ERROR] {result['source']}: {result['error']}", file=sys.stderr)
            continue
        if len(collected) > 1:
            print(f"\n===== {result['source']} "
                  f"[{result['engine']}, {result['elapsed_seconds']}s, "
                  f"{result['image']['width']}x{result['image']['height']}] =====")
        for warning in result.get("warnings") or []:
            print(f"[warn] {warning}", file=sys.stderr)
        print(result["text"])
        if result.get("saved_to"):
            print(f"[saved] {result['saved_to']}", file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
