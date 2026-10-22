"""本地 Web MCP server（stdio）：给 Agent 提供浏览器式的网页读取能力。

定位：补齐六件套里缺失的 browser 工具。第一版做"抓取 + 正文提取"（适合
查文档、读文章、看接口响应）；不做 JS 渲染与交互。需要真·浏览器自动化
（点击/截图/登录态页面）时，在管理端另注册 @playwright/mcp 即可，两者并存。

注册方式：管理端 MCP 页面新增 stdio 服务，command=python，
args=["<backend>/app/agents/mcp_servers/web_mcp_server.py"]。
安全约束：只允许 http/https；单响应最多读 3MB；正文截断到 2 万字符。
"""
import re
from html.parser import HTMLParser

import httpx
# MCP SDK 双版本兼容（1.x FastMCP / 2.x MCPServer）
try:
    from mcp.server.fastmcp import FastMCP as _MCP
except Exception:
    from mcp.server.mcpserver import MCPServer as _MCP

mcp = _MCP("web")

_MAX_BODY_BYTES = 3 * 1024 * 1024
_MAX_TEXT_CHARS = 20000
_TIMEOUT = 20.0
_UA = "Mozilla/5.0 (compatible; AgentPlatformBot/1.0)"

_SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "head", "iframe"}
_BLOCK_TAGS = {
    "p", "div", "section", "article", "li", "tr", "br", "h1", "h2", "h3",
    "h4", "h5", "h6", "table", "ul", "ol", "blockquote", "pre", "footer",
    "header", "nav", "main",
}


class _TextExtractor(HTMLParser):
    """朴素的 HTML→文本：跳过脚本样式，块级标签换行，压缩空白。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n")
        if tag in ("td", "th"):
            self._parts.append(" | ")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data):
        if self._skip_depth == 0 and data.strip():
            self._parts.append(data)

    def text(self) -> str:
        raw = "".join(self._parts)
        lines = [re.sub(r"[ \t\u3000]+", " ", ln).strip() for ln in raw.splitlines()]
        out, blank = [], 0
        for ln in lines:
            if ln:
                out.append(ln)
                blank = 0
            else:
                blank += 1
                if blank == 1 and out:
                    out.append("")
        return "\n".join(out).strip()


def _html_to_text(html: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(html)
    except Exception:
        return re.sub(r"<[^>]+>", " ", html)
    return parser.text()


def _truncate(text: str) -> str:
    if len(text) <= _MAX_TEXT_CHARS:
        return text
    return text[:_MAX_TEXT_CHARS] + f"\n…[正文过长，已截断，共 {len(text)} 字符]"


@mcp.tool()
async def web_fetch(url: str) -> str:
    """抓取一个网页并返回可读正文（HTML 会自动转纯文本；JSON/纯文本原样返回）。
    适合查文档、读文章、看公开接口响应。不支持需要登录或 JS 渲染的页面。

    Args:
        url: 以 http:// 或 https:// 开头的完整网址。
    """
    if not re.match(r"^https?://", url or "", re.I):
        return "Error: 只支持 http/https 网址。"
    try:
        async with httpx.AsyncClient(
            timeout=_TIMEOUT, follow_redirects=True,
            headers={"User-Agent": _UA},
        ) as client:
            resp = await client.get(url)
    except httpx.TimeoutException:
        return f"Error: 请求超时（{_TIMEOUT:.0f}s）: {url}"
    except Exception as e:
        return f"Error: 请求失败: {type(e).__name__}: {e}"

    ctype = (resp.headers.get("content-type") or "").lower()
    body = resp.content[:_MAX_BODY_BYTES]
    truncated_note = ""
    if len(resp.content) > _MAX_BODY_BYTES:
        truncated_note = "\n[响应体超过 3MB，仅处理前 3MB]"

    if "html" in ctype:
        text = _html_to_text(body.decode(resp.encoding or "utf-8", errors="replace"))
    else:
        text = body.decode("utf-8", errors="replace")

    header = f"[HTTP {resp.status_code}] {resp.url}\n"
    return _truncate(header + text + truncated_note)


@mcp.tool()
async def web_links(url: str) -> str:
    """列出页面里的全部链接（文本 + href），用于了解站点结构或挑选下一步要读的页面。

    Args:
        url: 以 http:// 或 https:// 开头的完整网址。
    """
    if not re.match(r"^https?://", url or "", re.I):
        return "Error: 只支持 http/https 网址。"
    try:
        async with httpx.AsyncClient(
            timeout=_TIMEOUT, follow_redirects=True,
            headers={"User-Agent": _UA},
        ) as client:
            resp = await client.get(url)
    except Exception as e:
        return f"Error: 请求失败: {type(e).__name__}: {e}"

    html = resp.content[:_MAX_BODY_BYTES].decode(resp.encoding or "utf-8", errors="replace")
    links = re.findall(
        r'<a\s[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
        html, re.I | re.S,
    )
    lines = []
    for href, inner in links[:300]:
        label = re.sub(r"<[^>]+>", "", inner)
        label = re.sub(r"\s+", " ", label).strip()[:80]
        if href.startswith(("http", "/")) or href.startswith("./"):
            lines.append(f"- {label or '(无文本)'}: {href}")
    return _truncate(f"[HTTP {resp.status_code}] {resp.url} 共 {len(links)} 个链接\n" + "\n".join(lines))


if __name__ == "__main__":
    mcp.run()
