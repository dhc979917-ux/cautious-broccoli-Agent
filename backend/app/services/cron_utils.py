"""cron 表达式工具：中文人性化描述 + APScheduler 触发器构造（纯函数、无状态、无 IO）。

语义约定（本模块是唯一事实源，改之前先读这里）：

1. 对外一律使用**标准 crontab 语义**：day-of-week 的 0 与 7 都是周日、1=周一 … 6=周六。
   数据库 ``scheduled_jobs.cron_expr``、前端星期选项、中文描述都按这个语义存取。
2. APScheduler 3.x 的 ``CronTrigger`` 把 0 当**周一**（其 ``from_crontab`` docstring 明确
   承认的历史遗留问题，4.x 才修正）。因此构造触发器前必须整体转换星期字段
   （见 build_cron_trigger），否则"每周五"会实际在周六触发、
   "工作日 1-5"会变成周二到周六。
3. 无法识别的写法：describe_cron 回退为 ``Cron: <原文>``；build_cron_trigger 抛
   ``ValueError``（API 层转 400）。两者都不会静默按错误语义执行。

支持的字段写法：``*``、单值、逗号列表、区间 ``a-b``、步长 ``*/n``、``a/n``、``a-b/n``，
星期字段另外支持英文名（``mon`` / ``monday`` …）。
"""
from typing import Dict, List, Optional, Tuple

from apscheduler.triggers.cron import CronTrigger

# 周一..周日，与 Python datetime.weekday() 同序（render_prompt 的 {{weekday}} 也用它）
WEEKDAY_ZH = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
WORKDAY_ZH = WEEKDAY_ZH[:5]

_DOW_NAMES: Dict[str, int] = {
    "sun": 0, "sunday": 0, "mon": 1, "monday": 1, "tue": 2, "tuesday": 2,
    "wed": 3, "wednesday": 3, "thu": 4, "thursday": 4, "fri": 5, "friday": 5,
    "sat": 6, "saturday": 6,
}
_MINUTE_RANGE = (0, 59)
_HOUR_RANGE = (0, 23)
_DAY_RANGE = (1, 31)
# crontab 的星期允许 0 与 7 都表示周日，故上界取 7
_DOW_RANGE = (0, 7)
# 描述文案里最多平铺多少个时间点 / 日期，超出则回退原文，避免表格被撑爆
_MAX_CLOCK_POINTS = 6
_MAX_MONTH_DAYS = 6


# ---------------- 字段解析 ----------------

def split_cron(expr: str) -> Optional[Tuple[str, str, str, str, str]]:
    """拆成 (分, 时, 日, 月, 周) 五段；段数不对返回 None"""
    parts = (expr or "").split()
    return (parts[0], parts[1], parts[2], parts[3], parts[4]) if len(parts) == 5 else None


def _parse_value(raw: str, lo: int, hi: int,
                 names: Optional[Dict[str, int]] = None) -> Optional[int]:
    """单个取值（数字或英文名）-> 整数；越界或不认识返回 None"""
    s = (raw or "").strip().lower()
    if s.isdigit():
        v = int(s)
        return v if lo <= v <= hi else None
    if names and s in names:
        return names[s]
    return None


def _expand_field(field: str, lo: int, hi: int,
                  names: Optional[Dict[str, int]] = None) -> Optional[List[int]]:
    """把一个 cron 字段展开成升序整数列表；无法识别返回 None"""
    field = (field or "").strip()
    if not field:
        return None
    values = set()
    for token in field.split(","):
        token = token.strip()
        if not token:
            return None
        body, step = token, 1
        if "/" in token:
            body, _, raw_step = token.partition("/")
            if not raw_step.strip().isdigit() or int(raw_step) < 1:
                return None
            step = int(raw_step)
        body = body.strip()
        if body == "*":
            start, end = lo, hi
        elif "-" in body:
            a, _, b = body.partition("-")
            start = _parse_value(a, lo, hi, names)
            end = _parse_value(b, lo, hi, names)
            if start is None or end is None:
                return None
        else:
            start = _parse_value(body, lo, hi, names)
            if start is None:
                return None
            # "5/2" 等价于 "5-上界/2"（Vixie cron 行为）
            end = hi if step > 1 else start
        if start > end:
            # 跨边界写法（如星期 6-1 = 周六到周一）：按环绕展开
            span = list(range(start, hi + 1)) + list(range(lo, end + 1))
        else:
            span = list(range(start, end + 1))
        values.update(span[::step])
    return sorted(values)


def expand_cron_dow(field: str) -> Optional[List[int]]:
    """crontab 星期字段 -> 归一化到 0-6（0=周日）的升序列表；无法识别返回 None"""
    days = _expand_field(field, *_DOW_RANGE, _DOW_NAMES)
    return sorted({d % 7 for d in days}) if days is not None else None


# ---------------- 触发器构造（星期语义转换的唯一入口） ----------------

def weekday_zh(crontab_dow: int) -> str:
    """crontab 星期数字（0/7=周日）-> 中文名"""
    return WEEKDAY_ZH[(crontab_dow - 1) % 7]


def _dow_field_to_apscheduler(field: str) -> Optional[str]:
    """crontab 星期字段 -> APScheduler 星期字段（0=周一 … 6=周日）；无法识别返回 None"""
    days = expand_cron_dow(field)
    if days is None:
        return None
    ap = sorted({(d - 1) % 7 for d in days})
    if len(ap) == 7:
        return "*"
    if len(ap) > 1 and ap == list(range(ap[0], ap[-1] + 1)):
        return f"{ap[0]}-{ap[-1]}"
    return ",".join(str(x) for x in ap)


def _check_timezone(timezone) -> None:
    """时区字符串预校验：不合法抛 ValueError（而不是 ZoneInfoNotFoundError/KeyError）"""
    if not isinstance(timezone, str):
        return
    from zoneinfo import ZoneInfo
    try:
        ZoneInfo(timezone)
    except Exception as e:  # ZoneInfoNotFoundError 是 KeyError 子类，统一收口
        raise ValueError(f"未知时区: {timezone}") from e


def build_cron_trigger(expr: str, timezone=None) -> CronTrigger:
    """按标准 crontab 语义构造 CronTrigger（内部完成星期字段的语义转换）。

    与 ``CronTrigger.from_crontab`` 的唯一区别就是星期字段：后者把 crontab 的
    0=周日 直接喂给按 0=周一 解释的 APScheduler，导致所有每周任务偏一天。
    表达式或时区非法时抛 ``ValueError``。
    """
    fields = split_cron(expr)
    if fields is None:
        raise ValueError(f"cron 表达式必须是 5 段（分 时 日 月 周）: {expr!r}")
    minute, hour, day, month, dow = fields
    ap_dow = _dow_field_to_apscheduler(dow)
    if ap_dow is None:
        raise ValueError(f"无法识别的星期字段: {dow!r}")
    _check_timezone(timezone)
    return CronTrigger(minute=minute, hour=hour, day=day, month=month,
                       day_of_week=ap_dow, timezone=timezone)


# ---------------- 中文描述 ----------------

def _clock_times(minute: str, hour: str) -> Optional[List[str]]:
    """分/时 两段 -> HH:MM 点位列表；含通配或点位过多返回 None（回退原文）"""
    if "*" in minute or "*" in hour:
        return None
    mins = _expand_field(minute, *_MINUTE_RANGE)
    hours = _expand_field(hour, *_HOUR_RANGE)
    if mins is None or hours is None or len(mins) * len(hours) > _MAX_CLOCK_POINTS:
        return None
    return [f"{h:02d}:{m:02d}" for h in hours for m in mins]


def _weekday_names(dow: str) -> Optional[List[str]]:
    """星期字段 -> 中文名列表（周一..周日顺序）；不限制星期或无法识别返回 None"""
    if (dow or "").strip() in ("*", ""):
        return None
    days = expand_cron_dow(dow)
    if not days or len(days) == 7:
        return None
    ordered = sorted(days, key=lambda d: (d - 1) % 7)
    return [weekday_zh(d) for d in ordered]


def _month_days(dom: str) -> Optional[List[str]]:
    """日字段 -> 日期字符串列表；不限制、无法识别或过多时返回 None"""
    if (dom or "").strip() in ("*", ""):
        return None
    days = _expand_field(dom, *_DAY_RANGE)
    if not days or len(days) > _MAX_MONTH_DAYS:
        return None
    return [str(d) for d in days]


def describe_cron(expr: str) -> str:
    """常见 cron 表达式的中文人性化描述（按 crontab 语义解读）；无法识别回退原文。

    覆盖：每天 / 每周X / 每周一、周三 / 每个工作日 / 每月 X 日 / 一天多个时间点。
    """
    fields = split_cron(expr)
    if fields is None:
        return f"Cron: {expr}"
    minute, hour, dom, mon, dow = fields
    if mon.strip() != "*":          # 限定月份的表达式不平铺描述
        return f"Cron: {expr}"
    times = _clock_times(minute, hour)
    if not times:
        return f"Cron: {expr}"
    when = "、".join(times)

    weekdays = _weekday_names(dow)
    month_days = _month_days(dom)
    if weekdays and not month_days:
        if weekdays == WORKDAY_ZH:
            return f"每个工作日 {when}"
        return f"每{'、'.join(weekdays)} {when}"
    if month_days and not weekdays:
        return f"每月 {'、'.join(month_days)} 日 {when}"
    if not month_days and not weekdays:
        return f"每天 {when}"
    return f"Cron: {expr}"
