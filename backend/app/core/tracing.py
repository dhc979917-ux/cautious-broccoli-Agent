"""OpenTelemetry 追踪初始化模块。
提供按天轮转的文件型 Span 导出器（JSON Lines）和 setup_tracing() 初始化函数。
TracingMiddleware 依赖此模块完成 TracerProvider 注册；若未调用 setup_tracing()，
TracingMiddleware 会自动降级为 no-op，不影响正常运行。

文件布局（BASE_DIR/traces/）：
- traces-YYYYMMDD.jsonl  当天 span（新命名，按天轮转）
- traces.jsonl           历史遗留单文件（只读兼容，按 mtime 参与过期清理）
过期清理：超过 TRACE_RETENTION_DAYS 天的文件删除；每小时最多执行一次。
大字段截断：span 属性里的超长字符串（工具结果/工具定义）保留头尾、截断中间，
控制单行体积（曾出现单条 span 600KB+ 的情况）。
"""

import logging

logger = logging.getLogger(__name__)

import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

from app.config import BASE_DIR, settings

_TRACE_FILE_RE = re.compile(r"^traces-(\d{8})\.jsonl$")
_HEAD = 4000
_TAIL = 1000


def trace_dir_path() -> Path:
    # 容器化部署可用 TRACES_DIR 环境变量重定向到宿主机挂载目录（同路径挂载）
    return Path(os.environ.get("TRACES_DIR", str(BASE_DIR / "traces")))


def list_trace_files(trace_dir: Path = None) -> list[Path]:
    """trace 目录下所有 span 文件（含遗留 traces.jsonl），按文件名排序。"""
    d = trace_dir or trace_dir_path()
    if not d.is_dir():
        return []
    files = [p for p in d.iterdir() if p.is_file() and _TRACE_FILE_RE.match(p.name)]
    legacy = d / "traces.jsonl"
    if legacy.is_file():
        files.append(legacy)
    return sorted(files, key=lambda p: p.name)


def _truncate_value(v):
    """超长字符串属性保留头尾截断（token 用量等数值属性不受影响）。"""
    if isinstance(v, str) and len(v) > settings.TRACE_MAX_ATTR_CHARS:
        omitted = len(v) - _HEAD - _TAIL
        return v[:_HEAD] + f"\n…[中间省略 {omitted} 字符]…\n" + v[-_TAIL:]
    return v


class RotatingFileSpanExporter(SpanExporter):
    """按天轮转的 JSON Lines span 导出器（含过期清理与大字段截断）。"""

    def __init__(self, trace_dir: Path):
        self._dir = trace_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._last_purge_day = ""

    def _today_file(self) -> Path:
        day = datetime.now().strftime("%Y%m%d")
        return self._dir / f"traces-{day}.jsonl"

    def export(self, spans: list[ReadableSpan]) -> SpanExportResult:
        try:
            lines = [json.dumps(self._span_to_dict(s), ensure_ascii=False) for s in spans]
            with self._lock:
                with open(self._today_file(), "a", encoding="utf-8") as f:
                    f.write("\n".join(lines) + "\n")
                self._purge_expired_locked()
            return SpanExportResult.SUCCESS
        except Exception as e:
            logger.error(f"[Tracing] 写入 trace 文件失败: {e}")
            return SpanExportResult.FAILURE

    def _purge_expired_locked(self) -> None:
        """每天最多清理一次：治理开启后改为受控审批清理。"""
        if settings.GOVERNANCE_OPERATIONS_ENABLED:
            return
        today = datetime.now().strftime("%Y%m%d")
        if today == self._last_purge_day:
            return
        self._last_purge_day = today
        try:
            cutoff = datetime.now().timestamp() - settings.TRACE_RETENTION_DAYS * 86400
            for p in list_trace_files(self._dir):
                m = _TRACE_FILE_RE.match(p.name)
                if m:
                    file_ts = datetime.strptime(m.group(1), "%Y%m%d").timestamp()
                else:
                    file_ts = p.stat().st_mtime  # 遗留 traces.jsonl 按修改时间
                if file_ts < cutoff:
                    p.unlink(missing_ok=True)
                    logger.info(f"[Tracing] 已清理过期 trace 文件: {p.name}")
        except Exception as e:
            logger.error(f"[Tracing] 过期清理失败（不影响写入）: {e}")

    def shutdown(self) -> None:
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True

    @staticmethod
    def _span_to_dict(span: ReadableSpan) -> dict:
        ctx = span.get_span_context()
        parent = span.parent
        start_ns = span.start_time or 0
        end_ns = span.end_time or 0
        duration_ms = round((end_ns - start_ns) / 1_000_000, 2) if start_ns and end_ns else 0
        attributes = {key: _truncate_value(value) for key, value in (span.attributes or {}).items()}
        if settings.GOVERNANCE_OPERATIONS_ENABLED:
            from app.services.operations import safe_attributes
            attributes = safe_attributes(attributes)

        return {
            "trace_id": format(ctx.trace_id, "032x"),
            "span_id": format(ctx.span_id, "016x"),
            "parent_span_id": format(parent.span_id, "016x") if parent else None,
            "name": span.name,
            "start_time": datetime.fromtimestamp(start_ns / 1e9, tz=timezone.utc).isoformat() if start_ns else None,
            "end_time": datetime.fromtimestamp(end_ns / 1e9, tz=timezone.utc).isoformat() if end_ns else None,
            "duration_ms": duration_ms,
            "status": span.status.status_code.name if span.status else "UNSET",
            "attributes": attributes,
        }


# 兼容旧引用
FileSpanExporter = RotatingFileSpanExporter


def setup_tracing(trace_dir: Path = None) -> None:
    """初始化 OpenTelemetry TracerProvider，启用 TracingMiddleware。
    span 写入 BASE_DIR/traces/traces-YYYYMMDD.jsonl（按天轮转，保留 TRACE_RETENTION_DAYS 天）。
    """
    from opentelemetry import trace as otel_trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    if trace_dir is None:
        trace_dir = trace_dir_path()

    provider = TracerProvider()
    provider.add_span_processor(BatchSpanProcessor(RotatingFileSpanExporter(trace_dir)))
    otel_trace.set_tracer_provider(provider)
    logger.info(f"[Tracing] 已启用，trace 目录: {trace_dir}（保留 {settings.TRACE_RETENTION_DAYS} 天）")
