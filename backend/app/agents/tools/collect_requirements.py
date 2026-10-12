"""CollectRequirements 工具：动手前的一次性批量需求采集（官方外部执行机制）。

与 AskUserChoice 的分工（不要合并，两者语义不同）：
- AskUserChoice：运行中出现多方案分歧，请用户在若干**已知选项**里挑一个；
- CollectRequirements：动手前采集**未知事实**（公式、规格、口径、环境），
  答案空间无法枚举成选项，且需要一次问全多题。

为什么必须"一次问全"：本平台每轮推理都要重发全部上下文，N 次单题追问的成本是
N 轮 × 全上下文；一张表单是 1 轮。这是整个改造省 token 的核心杠杆。

每题强制带 assume（模型自己的最优猜测），因此：
- 用户可以一键全部用默认，不阻塞；
- 超时/跳过时按 assume 继续，并由提示词要求把 assume 记为"假设，待确认"；
- 不会出现"问了没人答就卡死"的死锁。

标记 is_external_tool=True：模型调用时 AgentScope 不执行它，而是发出
RequireExternalExecutionEvent 暂停回复；manager._resolve_external_execution
经 InteractionChannel 把表单推给前端，用户提交后组装 ExternalExecutionResultEvent
恢复执行（与 AskUserChoice 完全同一条链路，只是多一个分支）。
"""
from typing import Any

from agentscope.tool._base import ToolBase
from agentscope.permission import (
    PermissionBehavior,
    PermissionContext,
    PermissionDecision,
)

# 题数硬上限：超过 8 题用户填不完，且回传结果会撑大上下文
# 题数硬上限：超过 4 题用户填不完，且回传结果会撑大上下文；
# 同时 4 题以内的 JSON 参数不会因 LLM 输出 token 限制被截断
MAX_QUESTIONS = 4
MIN_QUESTIONS = 1


def normalize_questions(raw: Any) -> list[dict]:
    """把模型给的 questions 规整成前端可直接渲染的结构（缺字段补默认、砍超限）。

    放在工具模块而不是 manager：格式与 input_schema 同源，改 schema 时不会漏改。
    """
    out: list[dict] = []
    if not isinstance(raw, list):
        return out
    for i, q in enumerate(raw[:MAX_QUESTIONS]):
        if not isinstance(q, dict):
            continue
        qid = str(q.get("id") or f"q{i + 1}").strip() or f"q{i + 1}"
        qtype = str(q.get("type") or "text").strip().lower()
        if qtype not in ("choice", "multi", "text", "number"):
            qtype = "text"
        # 模型偶尔把选项写成 {"label": ...} 对象而不是字符串；直接 str() 会把
        # 整个 dict 变成选项文本，逼它重试一轮（白白多花一次全上下文重发）。
        options = []
        for o in (q.get("options") or []):
            if isinstance(o, dict):
                label = str(o.get("label") or o.get("value") or "").strip()
            else:
                label = str(o).strip()
            if label and label not in options:
                # 截断每个选项到 40 字：放在 normalize 而不是 schema maxLength，
                # 避免 LLM 生成超长选项时 schema 校验直接拒绝、浪费一整轮重试
                options.append(label[:40])
        options = options[:6]
        if qtype in ("choice", "multi") and len(options) < 2:
            # 选项不足两个的单选/多选题没有意义，退化成文本题
            qtype = "text"
        out.append({
            "id": qid,
            "label": str(q.get("label") or qid).strip()[:30],
            "type": qtype,
            "options": options,
            "placeholder": str(q.get("placeholder") or "").strip()[:120],
            "unit": str(q.get("unit") or "").strip()[:24],
            "required": bool(q.get("required")),
            "assume": str(q.get("assume") or "").strip()[:300],
            # choice/multi 默认允许自定义输入：用户可在选项外自由作答
            "allow_other": q.get("allow_other", True) if qtype in ("choice", "multi") else bool(q.get("allow_other")),
            "multiline": bool(q.get("multiline")),
        })
    return out


def format_result(questions: list[dict], raw_answer: Any, mode: str = "intake") -> str:
    """把用户提交的表单答案整理成紧凑的工具结果文本。

    只回传"题目: 取值"，未作答的用 assume 兜底并单列成假设清单——
    这份清单正是提示词要求写进产物规格说明与最终回复的内容。
    控制在数百 token 内，不回传原始 JSON（省上下文）。
    """
    import json

    answers: dict = {}
    if isinstance(raw_answer, dict):
        answers = raw_answer
    elif isinstance(raw_answer, str) and raw_answer.strip():
        try:
            parsed = json.loads(raw_answer)
            if isinstance(parsed, dict):
                answers = parsed
        except (ValueError, TypeError):
            answers = {}

    confirmed: list[str] = []
    assumed: list[str] = []
    for q in questions:
        qid = q.get("id")
        label = q.get("label") or qid
        val = answers.get(qid)
        if isinstance(val, list):
            val = "、".join(str(v) for v in val if str(v).strip())
        val = str(val).strip() if val is not None else ""
        if val:
            _opts = q.get("options") or []
            _custom = bool(q.get("allow_other")) and val not in _opts
            _tag = "（自定义）" if _custom else ""
            confirmed.append(f"- {label}: {val[:200]}{_tag}")
        else:
            guess = str(q.get("assume") or "").strip()
            assumed.append(
                f"- {label}: "
                + (guess[:200] if guess else "（未提供默认值，按行业常规取值并标注为假设）")
            )

    lines = [
        f"{'[interview] ' if mode == 'interview' else ''}需求采集完成：{len(questions)} 题，用户确认 {len(confirmed)} 项，"
        f"按假设处理 {len(assumed)} 项。"
    ]
    if confirmed:
        lines.append("【已确认】")
        lines.extend(confirmed)
    if assumed:
        lines.append("【假设，待确认】")
        lines.extend(assumed)
    lines.append(
        "请据此实现：把上面全部取值集中写进产物唯一的配置块，"
        "逐项标注「已确认」或「假设，待确认」；最终回复末尾列出待确认清单。"
    )
    return "\n".join(lines)


class CollectRequirements(ToolBase):
    """一次性向用户采集交付物所需的领域事实（外部执行，由平台桥接）。"""

    name: str = "CollectRequirements"

    description: str = (
        "Collect domain facts from the user in ONE batched form before writing "
        "any artifact code. Use once per artifact task, before the first line "
        "of output code, when correctness depends on facts you don't have: "
        "formulas, spec limits, environment, business rules. "
        "Ask 3-4 questions in a single call; NEVER split into multiple rounds. "
        "Only ask facts that change the implementation - never ask about "
        "colors, fonts, spacing or layout taste. "
        "Every question MUST carry 'assume': your best guess used when the "
        "user leaves it blank, so the task never blocks. "
        "Each option MUST be <= 40 chars. choice/multi questions allow custom "
        "input by default. "
        "Do not use this for answer-only or document-only tasks."
        " In interview mode (grill-me), call once per round with the whole frontier."
    )

    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "context": {
                "type": "string",
                "description": (
                    "一句话说明为什么需要这些信息、准备做什么（≤80 字）。"
                ),
            },
            "mode": {
                "type": "string",
                "enum": ["intake", "interview"],
                "description": "intake=动手前一次性采集（默认，artifact 档）；interview=访谈式多轮设计拷问（grill-me 等，每轮抛整条 frontier，不受'只调用一次'约束）。",
            },
            "questions": {
                "type": "array",
                "minItems": MIN_QUESTIONS,
                "maxItems": MAX_QUESTIONS,
                "description": f"{MIN_QUESTIONS}~{MAX_QUESTIONS} 个问题，一次问全。",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "string",
                            "description": "稳定标识（snake_case），回传时作为键。",
                        },
                        "label": {
                            "type": "string",
                            "description": "问题标题（≤30 字，问具体事实）。",
                        },
                        "type": {
                            "type": "string",
                            "enum": ["choice", "multi", "text", "number"],
                            "description": (
                                "作答形式：choice 单选 / multi 多选 / text 文本 / "
                                "number 数值。无法枚举答案空间时用 text。"
                            ),
                        },
                        "options": {
                            "type": "array",
                            "maxItems": 6,
                            "description": "choice/multi 时的候选项（2~6 个）。选项文本会自动截断到40字，无需手动控制长度。",
                            "items": {"type": "string"},
                        },
                        "placeholder": {
                            "type": "string",
                            "description": "text/number 的填写示例（如 (Max-Min)/(Max+Min)）。",
                        },
                        "unit": {
                            "type": "string",
                            "description": "number 的单位（如 μm / ℃ / 天），可省略。",
                        },
                        "required": {
                            "type": "boolean",
                            "description": "是否必答；默认 false（不答即用 assume）。",
                        },
                        "assume": {
                            "type": "string",
                            "maxLength": 200,
                            "description": (
                                "必填：用户不作答时采用的默认值（你的最优猜测，≤200字）。"
                                "它会被记为假设并出现在交付说明的待确认清单里。"
                            ),
                        },
                        "allow_other": {
                            "type": "boolean",
                            "description": "choice/multi 是否允许在选项外自定义作答（默认 true）。",
                        },
                        "multiline": {
                            "type": "boolean",
                            "description": "text 是否多行输入（适合需要较长说明的开放题）。",
                        },
                    },
                    "required": ["id", "label", "type", "assume"],
                },
            },
        },
        "required": ["context", "questions"],
    }

    # 外部执行工具：agent 不直接执行，暂停回复后由平台（WebSocket 桥）处理；
    # 等待用户填写可能长达数分钟，不允许并发；只读（不产生副作用）
    is_concurrency_safe: bool = False
    is_read_only: bool = True
    is_external_tool: bool = True

    async def check_permissions(
        self,
        tool_input: dict[str, Any],
        context: PermissionContext,
    ) -> PermissionDecision:
        """向用户采集需求不产生副作用，始终放行。"""
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message="CollectRequirements is always allowed.",
        )
