"""交付档位（delivery profile）：先问再做 / 做完再看 全部开关的单一真相源。

为什么单独成模块（低耦合）：
- 提示词拼装（architectures/base.py）、预算中间件（proactive_budget /
  session_budget）、质量指标中间件都要读同一个档位，若各自判定口径必然漂移；
- 判定逻辑只有两条来源：CreatePlan 显式声明（模型自报）与用户消息关键词兜底
  （模型没走计划流程时），两者都收敛在这里，别处只读不判。

档位语义：
- ANSWER   纯问答/分析/查询，无可交付文件         → 不采集、不验证
- DOCUMENT 文档类交付（docx/xlsx/pptx/pdf/md）    → 不采集、不验证
- ARTIFACT 可运行或可打开的产物（页面/看板/Web 应用/脚本工具/服务）
                                                → 强制采集 + 强制交付前验证

存放位置：``agent.state.middle_context[_CTX_KEY]``（与 SessionBudgetMiddleware
同款做法）。AgentState 随会话缓存存活；缓存被淘汰后由 BuildProfileMiddleware
在下一轮自动重判，自愈，不需要持久化到 DB。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)

# middle_context 里的键名（独占，不与其它中间件共用）
_CTX_KEY = "delivery_profile"


class DeliveryKind:
    """交付档位常量（用字符串常量而非 Enum：要直接进 JSON / trace 属性 / 提示词）。"""

    ANSWER = "answer"
    DOCUMENT = "document"
    ARTIFACT = "artifact"

    ALL = (ANSWER, DOCUMENT, ARTIFACT)


def is_valid(kind: Any) -> bool:
    return isinstance(kind, str) and kind in DeliveryKind.ALL


# ---------------------------------------------------------------------------
# 关键词兜底判定
# ---------------------------------------------------------------------------
# 只在模型没有显式声明档位时使用（简单任务不激活 task_mgmt，就不会调 CreatePlan）。
# 词表按"命中即定档"组织，新增场景只改这两个元组，判定函数不动。

# artifact：用户要一个能打开/能运行的东西
_ARTIFACT_WORDS = (
    "看板", "仪表盘", "大屏", "管理台", "后台管理", "单文件", "可视化页面",
    "柱状图", "折线图", "饼图", "图表", "画图", "绘图", "散点图", "条形图",
    "热力图", "雷达图", "数据图", "统计图", "可视化",
    "网页", "网站", "页面", "前端", "交互", "演示", "小程序", "小工具",
    "web应用", "web 应用", "应用", "系统", "工具", "平台", "站点",
    "dashboard", "html", "htm", "vue", "react", "tsx", "jsx", "demo",
    "webpage", "web app", "landing", "site", "prototype", "ui",
)

# document：交付物是文档而非可运行产物
_DOCUMENT_WORDS = (
    "报告", "文档", "总结", "汇报", "说明书", "手册", "纪要", "方案书",
    "ppt", "pptx", "word", "docx", "excel", "xlsx", "csv", "pdf",
    "markdown", " slides", "简报", "周报", "月报",
)

# 明确的问答信号（权重最低，仅用于都没有命中时不必强判）
_ANSWER_WORDS = (
    "为什么", "是什么", "解释", "分析", "对比", "翻译", "查询", "查一下",
    "帮我看看", "什么意思", "如何理解",
)

# 关键词按整词匹配中文没有词边界，统一用小写子串匹配；英文词加边界防误伤
# （例如 "ui" 不应命中 "build"）
_ASCII_WORD_RE_CACHE: dict[str, re.Pattern] = {}
_FILE_SUFFIX_RE = re.compile(r"\b(png|jpe?g|svg|csv|html?|gif|bmp|webp)\b", re.IGNORECASE)
_DRAW_VERB_RE = re.compile(r"(画|绘制|生成|导出|输出|保存为|做)\s*(一张|一个)?\s*(图|图表|柱状图|折线图|饼图|可视化)")
_BUILD_VERB_RE = re.compile(
    r"(做|制作|创建|新建|生成|编写|实现|开发|设计|搭建|修改|优化|重构|导出|输出|画|绘制|写)"
    r"|\b(build|create|generate|implement|make|code|design|develop|write|export|fix)\b",
    re.IGNORECASE,
)


def _has_file_intent(text: str) -> bool:
    """是否含产出可打开文件意图（画图/导出图片/保存为 png 等），命中即视为 artifact。"""
    if _FILE_SUFFIX_RE.search(text):
        return True
    return bool(_DRAW_VERB_RE.search(text))


def _hit(word: str, text: str) -> bool:
    """单个关键词是否命中。纯 ASCII 短词走词边界，其余走子串。

    ASCII 分支忽略大小写：调用方通常已把文本转小写，但直接调用时
    传入 "UI" 这类原文也必须能命中，否则判定结果依赖调用方是否预处理。
    """
    if not word:
        return False
    if word.isascii() and len(word) <= 4:
        pat = _ASCII_WORD_RE_CACHE.get(word)
        if pat is None:
            pat = re.compile(
                r"(?<![a-z0-9])" + re.escape(word.lower()) + r"(?![a-z0-9])",
                re.IGNORECASE,
            )
            _ASCII_WORD_RE_CACHE[word] = pat
        return bool(pat.search(text))
    return word in text.lower()


def _score(text: str, words: tuple[str, ...]) -> int:
    return sum(1 for w in words if _hit(w, text))


def detect_from_text(text: str) -> Optional[str]:
    """从用户消息文本推断档位；无法判定时返回 None（由调用方决定兜底）。

    规则：artifact 与 document 同时命中时 artifact 优先——构建型产物需要的
    谨慎程度是文档型的超集，误判成 artifact 的代价（多问一次、多验一次）
    远小于误判成 document 的代价（交付一个打不开的页面）。
    """
    if not text:
        return None
    low = text.lower()
    a = _score(low, _ARTIFACT_WORDS)
    answer = _score(low, _ANSWER_WORDS) or _score(low, ("介绍", "概述", "讲讲", "说说", "聊聊", "说明", "怎么"))
    if answer and not _BUILD_VERB_RE.search(low):
        return DeliveryKind.ANSWER
    if _has_file_intent(low):
        a += 1
    d = _score(low, _DOCUMENT_WORDS)
    if a and a >= d:
        return DeliveryKind.ARTIFACT
    if d:
        return DeliveryKind.DOCUMENT
    if answer:
        return DeliveryKind.ANSWER
    return None


# ---------------------------------------------------------------------------
# 档位读写（挂在 AgentState.middle_context 上）
# ---------------------------------------------------------------------------

def _store(agent_or_state: Any) -> Optional[dict]:
    """兼容传 agent 或传 AgentState：中间件手上有 agent，工具手上只有 state。"""
    target = getattr(agent_or_state, "state", agent_or_state)
    ctx = getattr(target, "middle_context", None)
    if not isinstance(ctx, dict):
        return None
    store = ctx.get(_CTX_KEY)
    if not isinstance(store, dict):
        store = {}
        ctx[_CTX_KEY] = store
    return store


def declare(agent: Any, kind: str) -> bool:
    """模型经 CreatePlan 显式声明档位（跨轮粘住，直到会话结束）。"""
    if not is_valid(kind):
        return False
    store = _store(agent)
    if store is None:
        return False
    store["declared"] = kind
    store["kind"] = kind
    store["source"] = "plan"
    return True


def get(agent: Any) -> str:
    """当前生效档位。未判定过时返回 ANSWER（最小惊讶：不触发额外开销）。"""
    store = _store(agent)
    if not store:
        return DeliveryKind.ANSWER
    kind = store.get("kind")
    return kind if is_valid(kind) else DeliveryKind.ANSWER


def get_source(agent: Any) -> str:
    store = _store(agent) or {}
    return str(store.get("source") or "none")


def resolve_for_reply(agent: Any, reply_id: str, user_text: str) -> str:
    """每轮回复开始时解析一次档位（同一轮内幂等）。

    优先级：CreatePlan 显式声明 > 本轮用户消息关键词 > 上一轮结论 > ANSWER。
    显式声明粘住不改：计划一旦定了交付物类型，后续追问轮不应因为一句
    "继续" 就掉回 answer 档。
    """
    store = _store(agent)
    if store is None:
        return DeliveryKind.ANSWER
    declared = store.get("declared")
    if is_valid(declared):
        store["kind"] = declared
        store["source"] = "plan"
        store["reply_id"] = reply_id
        return declared
    if store.get("reply_id") == reply_id and is_valid(store.get("kind")):
        return store["kind"]
    detected = detect_from_text(user_text)
    if detected is None:
        # 兜底沿用上一轮结论：多轮构建任务里用户常说"继续/再改一下"，
        # 这类消息本身不含档位信号，掉回 answer 会导致验证被跳过。
        detected = store.get("kind") if is_valid(store.get("kind")) else DeliveryKind.ANSWER
        source = "carry"
    else:
        source = "auto"
    store["kind"] = detected
    store["source"] = source
    store["reply_id"] = reply_id
    return detected


def snapshot(agent: Any) -> dict:
    """只读快照，供 trace 属性与监控上报使用。"""
    store = _store(agent) or {}
    return {
        "delivery_kind": get(agent),
        "profile_source": str(store.get("source") or "none"),
    }


def is_artifact(agent: Any) -> bool:
    return get(agent) == DeliveryKind.ARTIFACT
