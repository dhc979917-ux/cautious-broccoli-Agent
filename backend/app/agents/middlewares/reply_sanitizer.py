"""回复净化：把模型交给用户的"沙箱内地址"换成真正能打开的预览地址。

起因（会话 398）：Agent 在沙箱里起了 Flask 服务，然后把 http://localhost:8123
当成访问地址交给用户。沙箱是独立容器，用户机器上的 localhost 跟它毫无关系，
点开必然失败，用户只会认为平台给错了地址。

提示词层已经明令禁止（见 architectures/base.py 的执行环境规则），但模型遵从性
不稳定，所以在出口再做一次确定性兜底：
  - 命中已发布预览的端口 -> 直接替换成平台发布的真实地址；
  - 命中内部地址但没有对应预览 -> 换成一句明确说明，绝不留一个死链给用户。

实现上是纯函数、无状态、不依赖 AgentScope 中间件协议：chat.py 的三个出口
（正常结束 / 用户停止 / 异常失败）在落库前各调用一次即可，随时可摘掉。
"""

import logging
import re
from typing import Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

# 需要处理的主机形态：回环、通配、IPv6 回环、沙箱容器名、RFC1918 私网地址。
# 私网地址要额外判断——部署环境本身可能就在 10.x/192.168.x 上，
# 平台发布的预览地址（http://<平台主机>:31005/）必须原样放行，见 _is_allowed_host。
# 每段都写满 4 个八位组：写成 10\.\d+\.\d+ 会只吃掉 "10.9.7" 三截，
# 把剩下的 ".52:31005/" 留在原地，反而把正确地址改成半截错地址（实测踩过）。
_PRIVATE_IP = (
    r"10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r"|192\.168\.\d{1,3}\.\d{1,3}"
    r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
)
# 路径只收 RFC 3986 允许出现的字符。用 \S* 会把紧跟在 URL 后面的中文正文
# （"…/dashboard?id=1，注意结尾。"）整段吞掉，替换后正文就少了半句。
_PATH_CHARS = r"A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%"
_URL_RE = re.compile(
    r"https?://(?P<host>"
    r"localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\]|::1|as_ws_[0-9a-fA-F]{4,}"
    rf"|{_PRIVATE_IP}"
    r")(?::(?P<port>\d{1,5}))?(?P<path>/[" + _PATH_CHARS + r"]*)?",
    re.IGNORECASE,
)

# 中文语境里 URL 后面常紧跟标点，正则的 \S* 会把它吞进来，替换前要还回去
_TRAILING_PUNCT = "。，、；：！？）)】》>»\"'`,;:!?…"

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]"}

# 找不到对应预览时给出的替代文案：说清"为什么打不开"和"去哪儿拿正确地址"
_UNKNOWN_REPLACEMENT = (
    "【此处原为沙箱内部地址，用户无法访问；请以上方"
    "「服务预览」卡片给出的地址为准，或下载源码包自行部署】"
)


def _split_trailing(url: str) -> "tuple[str, str]":
    """把 URL 末尾粘连的标点切出来，返回 (纯 URL, 尾部标点)。"""
    idx = len(url)
    while idx > 0 and url[idx - 1] in _TRAILING_PUNCT:
        idx -= 1
    # 成对括号：markdown 链接 [text](http://...) 的收尾右括号不能算 URL 的一部分
    return url[:idx], url[idx:]


def _normalize_host(host: str) -> str:
    return (host or "").strip().strip("[]").lower()


def _collect_known(previews: Optional[Iterable[dict]]) -> "tuple[Dict[int, str], set]":
    """从已发布预览里整理出：端口 -> 真实地址，以及允许原样放行的主机集合。"""
    port_map: Dict[int, str] = {}
    allowed_hosts: set = set()
    for item in previews or []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        match = re.match(r"https?://([^/:]+)", url, re.IGNORECASE)
        if match:
            allowed_hosts.add(_normalize_host(match.group(1)))
        target_port = item.get("target_port")
        try:
            if target_port:
                # 同一端口多次发布时以最新（列表靠前）的为准
                port_map.setdefault(int(target_port), url)
        except (TypeError, ValueError):
            continue
    return port_map, allowed_hosts


def _is_allowed_host(host: str, allowed_hosts: set, base_host: str) -> bool:
    """平台自己的对外地址（预览基址主机）必须放行，否则会把正确答案也擦掉。"""
    if not host:
        return False
    if base_host and host == _normalize_host(base_host):
        return True
    return host in allowed_hosts


def sanitize_reply(
    text: Optional[str],
    previews: Optional[List[dict]] = None,
    base_url: str = "",
) -> str:
    """净化一段回复文本。没有命中时原样返回（不做任何多余改写）。

    Args:
        text:     模型回复正文
        previews: preview_service.preview_to_dict 的结果列表（本条消息关联的预览）
        base_url: 平台对外基址（如 http://your-host）；用于放行平台自己发布的地址
    """
    if not text:
        return text or ""
    port_map, allowed_hosts = _collect_known(previews)
    base_host = ""
    if base_url:
        match = re.match(r"https?://([^/:]+)", base_url.strip(), re.IGNORECASE)
        base_host = _normalize_host(match.group(1)) if match else _normalize_host(base_url)

    # 端口写错、但整段回复里只有这一个内部地址、且本轮只发布过一个预览时，
    # 大概率指的就是它，直接换成正确地址；有多个候选时宁可给提示也不猜错。
    fallback_url = ""
    if len(port_map) == 1 and len(list(_URL_RE.finditer(text))) == 1:
        fallback_url = next(iter(port_map.values()))

    replaced = 0

    def _sub(match: "re.Match") -> str:
        nonlocal replaced
        raw = match.group(0)
        url, tail = _split_trailing(raw)
        host = _normalize_host(match.group("host"))
        port = match.group("port")
        if _is_allowed_host(host, allowed_hosts, base_host):
            return raw
        # 内部地址：能对上预览端口就换成真地址，对不上就明确告知不可用
        target = ""
        if port:
            try:
                target = port_map.get(int(port), "")
            except ValueError:
                target = ""
        if not target and fallback_url and host in _LOOPBACK_HOSTS:
            target = fallback_url
        replacement = target or _UNKNOWN_REPLACEMENT
        replaced += 1
        return replacement + tail

    result = _URL_RE.sub(_sub, text)
    if replaced:
        logger.info(f"[reply-sanitizer] 已替换 {replaced} 处沙箱内部地址")
    return result
