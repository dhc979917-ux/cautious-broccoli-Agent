"""Trace 聚合分析：任务成本 + 工具耗时（读取 traces/ 目录的 JSONL span 文件）。

数据来源是 OTel TracingMiddleware 落盘的 span（chat / execute_tool / invoke_agent），
与 DB 解耦。按天轮转文件只读保留窗口内的部分；遗留 traces.jsonl 按 mtime 判断是否纳入。
口径说明：
- token 来自 chat span 的 gen_ai.usage.input_tokens / output_tokens；
- 缓存命中来自 chat span 的 agentscope.usage.cache_input_tokens（TracingMiddleware
  写入，只有非 0 才有该属性）。这是服务端真实命中率，与消息 metadata 里上报的
  token_usage 相互独立，可交叉校验；
- 任务耗时优先取 invoke_agent span 的 duration，缺失时用 span 起止范围估算；
- 工具耗时为 execute_tool span 的 duration 累加。
"""
import json
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from app.core.tracing import list_trace_files, trace_dir_path


def _parse_ts(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _iter_spans(trace_dir: Path, since: Optional[datetime]):
    """逐行产出保留窗口内的 span dict；坏行跳过。"""
    for p in list_trace_files(trace_dir):
        if since is not None:
            # 轮转文件按文件名日期过滤；遗留文件按 mtime
            from datetime import datetime as _dt
            if p.name.startswith("traces-") and len(p.name) == len("traces-YYYYMMDD.jsonl"):
                try:
                    day = _dt.strptime(p.stem.split("-", 1)[1], "%Y%m%d")
                    if day.date() < since.date():
                        continue
                except Exception:
                    pass
            else:
                if datetime.fromtimestamp(p.stat().st_mtime) < since:
                    continue
        try:
            with open(p, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except Exception:
                        continue
        except Exception:
            continue


class TraceStatsService:
    def __init__(self, trace_dir: Path = None):
        self._dir = trace_dir or trace_dir_path()

    def _aggregate(self, days: int) -> dict:
        since = datetime.now() - timedelta(days=days)
        tasks: dict[str, dict] = {}
        tools: dict[str, dict] = defaultdict(
            lambda: {"count": 0, "total_ms": 0.0, "max_ms": 0.0}
        )
        for s in _iter_spans(self._dir, since):
            tid = s.get("trace_id") or "?"
            name = str(s.get("name") or "")
            attrs = s.get("attributes") or {}
            dur = float(s.get("duration_ms") or 0)
            t = tasks.setdefault(tid, {
                "trace_id": tid,
                "started_at": None,
                "_start_dt": None,
                "_end_dt": None,
                "model": None,
                "llm_rounds": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_input_tokens": 0,
                "tool_calls": 0,
                "tool_time_s": 0.0,
                "duration_s": 0.0,
                "status": "ok",
            })
            st = _parse_ts(s.get("start_time"))
            et = _parse_ts(s.get("end_time"))
            if st and (t["_start_dt"] is None or st < t["_start_dt"]):
                t["_start_dt"] = st
                t["started_at"] = s.get("start_time")
            if et and (t["_end_dt"] is None or et > t["_end_dt"]):
                t["_end_dt"] = et
            if str(s.get("status")) == "ERROR":
                t["status"] = "error"

            if name.startswith("chat"):
                t["llm_rounds"] += 1
                t["input_tokens"] += int(attrs.get("gen_ai.usage.input_tokens") or 0)
                t["output_tokens"] += int(attrs.get("gen_ai.usage.output_tokens") or 0)
                t["cache_input_tokens"] += int(
                    attrs.get("agentscope.usage.cache_input_tokens") or 0
                )
                if not t["model"]:
                    t["model"] = attrs.get("gen_ai.request.model")
            elif name.startswith("execute_tool"):
                tool = attrs.get("gen_ai.tool.name") or name.replace("execute_tool", "").strip() or "?"
                t["tool_calls"] += 1
                t["tool_time_s"] += dur / 1000
                rec = tools[tool]
                rec["count"] += 1
                rec["total_ms"] += dur
                rec["max_ms"] = max(rec["max_ms"], dur)
            elif name.startswith("invoke_agent") and dur > 0:
                # invoke_agent span 的时长就是整任务时长（优先采用）
                t["duration_s"] = max(t["duration_s"], dur / 1000)

        for t in tasks.values():
            if t["duration_s"] <= 0 and t["_start_dt"] and t["_end_dt"]:
                t["duration_s"] = round((t["_end_dt"] - t["_start_dt"]).total_seconds(), 1)
            t.pop("_start_dt", None)
            t.pop("_end_dt", None)
            t["tool_time_s"] = round(t["tool_time_s"], 1)
            # 真实缓存命中率（%）：cached 是 input 的子集（OpenAI 语义）
            t["cache_rate"] = (
                round(t["cache_input_tokens"] / t["input_tokens"] * 100, 1)
                if t["input_tokens"] else 0.0
            )
        return {"tasks": tasks, "tools": tools}

    def tasks(self, days: int = 7, limit: int = 100, sort: str = "input_tokens") -> list[dict]:
        """任务成本列表（默认按累计 input token 降序）。"""
        days = max(1, min(int(days or 7), 90))
        limit = max(1, min(int(limit or 100), 500))
        data = self._aggregate(days)
        rows = list(data["tasks"].values())
        key = sort if sort in ("input_tokens", "output_tokens", "cache_input_tokens",
                               "duration_s", "started_at", "tool_calls") else "input_tokens"
        rows.sort(key=lambda r: r.get(key) or 0, reverse=(key != "started_at"))
        return rows[:limit]

    def tool_profile(self, days: int = 7) -> list[dict]:
        """工具耗时画像：次数、平均/最大耗时。"""
        days = max(1, min(int(days or 7), 90))
        data = self._aggregate(days)
        rows = []
        for name, rec in data["tools"].items():
            rows.append({
                "tool": name,
                "count": rec["count"],
                "avg_s": round(rec["total_ms"] / rec["count"] / 1000, 2) if rec["count"] else 0,
                "max_s": round(rec["max_ms"] / 1000, 2),
                "total_s": round(rec["total_ms"] / 1000, 1),
            })
        rows.sort(key=lambda r: r["count"], reverse=True)
        return rows

    def summary(self, days: int = 7) -> dict:
        """窗口内汇总：任务数、模型调用、token 总量、最重任务。"""
        days = max(1, min(int(days or 7), 90))
        data = self._aggregate(days)
        rows = list(data["tasks"].values())
        total_in = sum(r["input_tokens"] for r in rows)
        total_out = sum(r["output_tokens"] for r in rows)
        total_cache = sum(r["cache_input_tokens"] for r in rows)
        top = sorted(rows, key=lambda r: r["input_tokens"], reverse=True)[:5]
        return {
            "days": days,
            "task_count": len(rows),
            "error_count": sum(1 for r in rows if r["status"] == "error"),
            "llm_calls": sum(r["llm_rounds"] for r in rows),
            "tool_calls": sum(r["tool_calls"] for r in rows),
            "total_input_tokens": total_in,
            "total_output_tokens": total_out,
            "total_cache_input_tokens": total_cache,
            "cache_rate": round(total_cache / total_in * 100, 1) if total_in else 0.0,
            "avg_input_per_task": round(total_in / len(rows)) if rows else 0,
            "top_tasks": [
                {k: r[k] for k in ("trace_id", "started_at", "model", "llm_rounds",
                                   "input_tokens", "output_tokens", "cache_input_tokens",
                                   "cache_rate", "tool_calls", "duration_s")}
                for r in top
            ],
        }
