"""Tier 0：静态检查（纯标准库，无浏览器，无网络）。

设计原则：
- 绝不抛异常到调用方，任何单项检查失败都降级成一条 warning；
- 只做"确定性问题"的判定，模糊的一律标 warning 而不是 error，
  避免模型为假阳性反复返工（返工才是最贵的）；
- 结果条数有上限，防止大产物把上下文撑爆。

覆盖的缺陷类型（都来自真实踩坑）：
1. CSS var(--x) 引用了从未定义过的变量 → 选中态/文字颜色失效，白字打在浅底上
2. JS 里 getElementById/querySelector 指向不存在的 id → 图表容器拿不到，整块空白
3. 外链 CDN 脚本/字体/样式 → 内网离线环境打开即白屏
4. 内联 onclick/onchange 调用了未定义的函数 → 交互一点就报错
5. 脚本语法错误（有 node 时用 node --check 验，无 node 时跳过）
6. 重复 id、标签未闭合、图表容器无高度
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
from html.parser import HTMLParser
from typing import Any

logger = logging.getLogger(__name__)

# 单类问题最多报多少条（超出只给计数），防止大产物刷屏撑爆上下文
_MAX_PER_KIND = 8
# 参与检查的最大文件字节数；超大产物只做前 N 字节的检查并标注
_MAX_SCAN_BYTES = 6 * 1024 * 1024

# 自闭合/空元素：不要求闭合标签
_VOID_ELEMENTS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}

_RE_SCRIPT_BLOCK = re.compile(
    r"<script\b([^>]*)>(.*?)</script\s*>", re.IGNORECASE | re.DOTALL,
)
_RE_STYLE_BLOCK = re.compile(
    r"<style\b[^>]*>(.*?)</style\s*>", re.IGNORECASE | re.DOTALL,
)
_RE_SCRIPT_SRC = re.compile(
    r"<script\b[^>]*\bsrc\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE,
)
_RE_LINK_HREF = re.compile(
    r"<link\b[^>]*\bhref\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE,
)
_RE_ID_ATTR = re.compile(r"\bid\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)
_RE_GET_BY_ID = re.compile(
    r"getElementById\(\s*[\"'`]([^\"'`]+)[\"'`]\s*\)",
)
_RE_QUERY_HASH = re.compile(r"querySelector(?:All)?\(\s*[\"'`]#([A-Za-z0-9_-]+)")
_RE_CSS_VAR_USE = re.compile(r"var\(\s*(--[A-Za-z0-9_-]+)")
_RE_CSS_VAR_DEF = re.compile(r"(--[A-Za-z0-9_-]+)\s*:")
_RE_INLINE_HANDLER = re.compile(
    r"\bon([a-z]+)\s*=\s*[\"']([^\"']*)[\"']", re.IGNORECASE,
)
# 元素开始标签（抓 id 与 class 的对应关系，供"图表容器有没有高度"判定用）
_RE_OPEN_TAG = re.compile(r"<([a-zA-Z][\w-]*)((?:\s+[^<>]*?)?)>", re.DOTALL)
# CSS 规则（选择器, 声明体）：@media 内的规则会被单独匹配到，无需特殊处理
_RE_CSS_RULE = re.compile(r"([^{}]+)\{([^{}]*)\}")
_RE_HEIGHT_DECL = re.compile(r"(?:^|[;\s])height\s*:")
_RE_CLASS_IN_SELECTOR = re.compile(r"\.([A-Za-z0-9_-]+)")
_RE_HANDLER_FN = re.compile(r"([A-Za-z_$][\w$]*)\s*\(")
_RE_HTTP_REF = re.compile(r"\b(?:https?:)?//[^\"'\s)<>]+", re.IGNORECASE)
_RE_FN_DECL = re.compile(
    r"(?:function\s+([A-Za-z_$][\w$]*)\s*\(|"
    r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:function|\([^)]*\)\s*=>|[A-Za-z_$][\w$]*\s*=>)|"
    r"(?:^|[\s;})])([A-Za-z_$][\w$]*)\s*:\s*function)",
    re.MULTILINE,
)


class _StructureParser(HTMLParser):
    """收集 id、标签闭合情况。用标准库解析器，不引入 bs4 依赖。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ids: list[str] = []
        self.dup_ids: list[str] = []
        self.stack: list[str] = []
        self.unclosed: list[str] = []
        self.mismatched: list[str] = []
        self._seen: set[str] = set()

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        for k, v in attrs:
            if (k or "").lower() == "id" and v:
                if v in self._seen:
                    self.dup_ids.append(v)
                else:
                    self._seen.add(v)
                    self.ids.append(v)
        if tag not in _VOID_ELEMENTS:
            self.stack.append(tag)

    def handle_startendtag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        for k, v in attrs:
            if (k or "").lower() == "id" and v:
                if v in self._seen:
                    self.dup_ids.append(v)
                else:
                    self._seen.add(v)
                    self.ids.append(v)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _VOID_ELEMENTS:
            return
        if not self.stack:
            self.mismatched.append(f"</{tag}> 无对应开始标签")
            return
        if self.stack[-1] == tag:
            self.stack.pop()
            return
        if tag in self.stack:
            # 中间有未闭合的标签：把它们记下来再弹出到当前标签
            while self.stack and self.stack[-1] != tag:
                self.unclosed.append(f"<{self.stack.pop()}> 未闭合")
            if self.stack:
                self.stack.pop()
        else:
            self.mismatched.append(f"</{tag}> 与 <{self.stack[-1]}> 不匹配")


def _cap(items: list[str], kind: str) -> list[str]:
    """限制单类问题条数，超出部分汇总成一行计数。"""
    if len(items) <= _MAX_PER_KIND:
        return items
    head = items[:_MAX_PER_KIND]
    head.append(f"…另有 {len(items) - _MAX_PER_KIND} 处同类{kind}问题（已省略）")
    return head


def _is_external(ref: str) -> bool:
    r = ref.strip()
    if not r:
        return False
    if r.startswith("//"):
        return True
    low = r.lower()
    return low.startswith("http://") or low.startswith("https://")


def _classes_with_height(style_text: str) -> set[str]:
    """收集所有"声明了 height 的规则"的选择器里出现的 class 名。

    必须按整条选择器取全部 class：`.map-card .map { height:360px }` 这种后代
    选择器，若只用单条正则从左往右匹配，`.map` 会被吞进选择器中段而漏掉，
    于是把本来有高度的容器误报成 0 高度（实测踩过）。宁可多收不可漏收。
    """
    out: set[str] = set()
    for selector, body in _RE_CSS_RULE.findall(style_text):
        if _RE_HEIGHT_DECL.search(body):
            out.update(_RE_CLASS_IN_SELECTOR.findall(selector))
    return out


def _node_syntax_errors(js: str) -> list[str]:
    """有 node 就用 node --check 验语法（最准），没有就跳过（不算失败）。"""
    node = shutil.which("node")
    if not node or not js.strip():
        return []
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(suffix=".js", text=True)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(js)
        proc = subprocess.run(
            [node, "--check", tmp_path],
            capture_output=True, text=True, timeout=20,
            encoding="utf-8", errors="replace",
        )
        if proc.returncode == 0:
            return []
        msg = (proc.stderr or proc.stdout or "").strip()
        # node 的错误首行最有用，其余（堆栈）丢掉省 token
        first = [ln for ln in msg.splitlines() if ln.strip()][:3]
        return ["脚本语法错误: " + " | ".join(x.strip() for x in first)]
    except Exception as e:  # node 缺失/超时都不该阻断验证
        logger.debug("[verify.tier0] node --check 跳过: %s", e)
        return []
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _chart_container_height_warnings(html: str) -> list[str]:
    """图表挂载点没有显式高度 → 渲染出 0 高度容器（图不显示的常见原因）。

    只在能确定"这个 id 被当图表容器用"且"CSS 里没有给它高度"时才报，
    避免对普通 div 误报。
    """
    out: list[str] = []
    chart_ids = set()
    for m in re.finditer(
        r"(?:echarts|Chart|chart)\s*[\.(]?\s*(?:init\s*\()?\s*"
        r"document\.getElementById\(\s*[\"'`]([^\"'`]+)[\"'`]",
        html,
    ):
        chart_ids.add(m.group(1))
    if not chart_ids:
        return out
    style_text = "\n".join(m.group(1) for m in _RE_STYLE_BLOCK.finditer(html))
    inline_style_ids = set()
    for m in re.finditer(r"<[^>]*\bid\s*=\s*[\"']([^\"']+)[\"'][^>]*\bstyle\s*=\s*[\"']([^\"']*)[\"']", html, re.IGNORECASE):
        if "height" in m.group(2).lower():
            inline_style_ids.add(m.group(1))
    # id -> 该元素的 class 列表：高度常常定义在 class 选择器上而不是 #id 上，
    # 只看 #id 会把 `.map-card .map { height:360px }` 这类写法误判成没高度。
    id_classes: dict[str, list[str]] = {}
    for m in _RE_OPEN_TAG.finditer(html):
        attrs = m.group(2) or ""
        idm = re.search(r"\bid\s*=\s*[\"']([^\"']+)[\"']", attrs, re.IGNORECASE)
        if not idm:
            continue
        clm = re.search(r"\bclass\s*=\s*[\"']([^\"']*)[\"']", attrs, re.IGNORECASE)
        id_classes[idm.group(1)] = clm.group(1).split() if clm else []
    classes_with_height = _classes_with_height(style_text)
    for cid in sorted(chart_ids):
        if cid in inline_style_ids:
            continue
        # CSS 里是否有针对该 id（或它所属 class）的 height 声明
        if re.search(r"#" + re.escape(cid) + r"\b[^}]*height\s*:", style_text):
            continue
        if any(c in classes_with_height for c in id_classes.get(cid, [])):
            continue
        # 通配/标签级高度规则也算已定义（继续放宽，宁可漏报不要误报）
        if re.search(r"(?:^|[\s,{}])(?:\*|div|canvas)\s*\{[^}]*height\s*:", style_text):
            continue
        out.append(f"图表容器 #{cid} 未定义高度，渲染出来可能是 0 高度（图不显示）")
    return out


def run(path: str, *, use_node: bool = True) -> dict[str, Any]:
    """执行 Tier 0 静态检查，返回结构化结论（不含截图）。"""
    errors: list[str] = []
    warnings: list[str] = []
    externals: list[str] = []
    meta: dict[str, Any] = {"path": path, "tier": 0}

    if not os.path.isfile(path):
        return {
            "ok": False,
            "errors": [f"文件不存在: {path}"],
            "warnings": [],
            "externals": [],
            "meta": meta,
        }

    size = os.path.getsize(path)
    meta["bytes"] = size
    truncated = size > _MAX_SCAN_BYTES
    if truncated:
        meta["truncated"] = True
        warnings.append(
            f"产物 {size // 1024}KB 超过静态检查上限 {_MAX_SCAN_BYTES // 1024 // 1024}MB，"
            "只检查了前段内容"
        )

    try:
        with open(path, "rb") as fh:
            raw = fh.read(_MAX_SCAN_BYTES + 1)
    except OSError as e:
        return {
            "ok": False,
            "errors": [f"文件读取失败: {e}"],
            "warnings": warnings,
            "externals": [],
            "meta": meta,
        }

    html = raw.decode("utf-8", errors="replace")
    low = html.lower()

    # ---- 1. 结构：id 收集 + 标签闭合 ----
    parser = _StructureParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception as e:
        warnings.append(f"HTML 结构解析中断（不影响其余检查）: {e}")
    known_ids = set(parser.ids)
    # 补上"只出现在 JS 模板串里的 id"：动态拼出来的 DOM（innerHTML = `... id="x" ...`）
    # HTMLParser 看不到，只认静态属性会把这类引用全判成悬空（实测误报）。
    # 宁可放宽也不要假阳性——假 error 会逼模型做无谓返工，那才是最贵的。
    known_ids |= set(_RE_ID_ATTR.findall(html))
    meta["id_count"] = len(parser.ids)
    if parser.dup_ids:
        errors.extend(_cap([f"重复 id: {i}" for i in dict.fromkeys(parser.dup_ids)], "重复 id"))
    struct_issues = parser.unclosed + parser.mismatched
    if struct_issues:
        warnings.extend(_cap(list(dict.fromkeys(struct_issues)), "标签闭合"))

    # ---- 2. 脚本块：语法 + id 引用 + 函数定义集合 ----
    scripts = [(attrs, body) for attrs, body in _RE_SCRIPT_BLOCK.findall(html)]
    inline_js = "\n".join(body for attrs, body in scripts if "src=" not in attrs.lower())
    defined_fns: set[str] = set()
    for m in _RE_FN_DECL.finditer(inline_js):
        for g in m.groups():
            if g:
                defined_fns.add(g)
    # 对象方法简写 / class 方法也算已定义（减少误报）
    for m in re.finditer(r"(?:^|[\s{,])([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*\{", inline_js, re.MULTILINE):
        defined_fns.add(m.group(1))

    if use_node and inline_js.strip():
        syn = _node_syntax_errors(inline_js)
        if syn:
            errors.extend(syn)
        else:
            meta["js_syntax"] = "ok"

    dangling: list[str] = []
    for m in _RE_GET_BY_ID.finditer(html):
        target = m.group(1)
        if target not in known_ids:
            dangling.append(f"getElementById('{target}') 指向不存在的 id")
    for m in _RE_QUERY_HASH.finditer(html):
        target = m.group(1)
        if target not in known_ids:
            dangling.append(f"querySelector('#{target}') 指向不存在的 id")
    if dangling:
        errors.extend(_cap(list(dict.fromkeys(dangling)), "id 引用"))

    # ---- 3. 内联事件处理器调用了未定义的函数 ----
    # 浏览器全局函数（alert/confirm 等）与 DOM API 不算未定义
    _BUILTIN = {
        "alert", "confirm", "prompt", "print", "setTimeout", "setInterval",
        "clearTimeout", "clearInterval", "requestAnimationFrame", "fetch",
        "encodeURIComponent", "decodeURIComponent", "JSON", "Math", "Number",
        "String", "Boolean", "Array", "Object", "Date", "parseInt", "parseFloat",
        "if", "for", "while", "switch", "catch", "return", "function", "typeof",
    }
    missing_fns: list[str] = []
    for _evt, body in _RE_INLINE_HANDLER.findall(html):
        for fn in _RE_HANDLER_FN.findall(body or ""):
            if fn in _BUILTIN or fn in defined_fns:
                continue
            missing_fns.append(f"内联事件调用未定义函数 {fn}()")
    if missing_fns:
        errors.extend(_cap(list(dict.fromkeys(missing_fns)), "未定义函数"))

    # ---- 4. CSS 变量：引用了但从未定义 ----
    style_text = "\n".join(m.group(1) for m in _RE_STYLE_BLOCK.finditer(html))
    # 内联 style 属性里的 var() 也算引用
    css_all = style_text + "\n" + html
    used_vars = set(_RE_CSS_VAR_USE.findall(css_all))
    defined_vars = set(_RE_CSS_VAR_DEF.findall(style_text))
    undefined_vars = sorted(used_vars - defined_vars)
    if undefined_vars:
        errors.extend(_cap(
            [f"CSS 变量 {v} 被引用但从未定义（相关样式会静默失效）" for v in undefined_vars],
            "CSS 变量",
        ))
    meta["css_vars"] = {"used": len(used_vars), "defined": len(defined_vars)}

    # ---- 5. 外链资源（离线/内网环境打不开）----
    for m in _RE_SCRIPT_SRC.finditer(html):
        ref = m.group(1)
        if _is_external(ref):
            externals.append(f"script src={ref}")
        elif not ref.lower().startswith(("data:", "blob:")):
            local = os.path.join(os.path.dirname(os.path.abspath(path)), ref.lstrip("./"))
            if not os.path.exists(local):
                errors.append(f"引用的本地脚本不存在: {ref}")
    for m in _RE_LINK_HREF.finditer(html):
        ref = m.group(1)
        if _is_external(ref):
            externals.append(f"link href={ref}")
    for m in re.finditer(r"@import\s+(?:url\()?[\"']?([^\"')]+)", style_text):
        if _is_external(m.group(1)):
            externals.append(f"css @import {m.group(1).strip()}")
    for m in re.finditer(r"url\(\s*[\"']?(https?:[^\"')\s]+)", style_text):
        externals.append(f"css url({m.group(1)})")
    externals = list(dict.fromkeys(externals))
    if externals:
        warnings.extend(_cap(
            [f"外链资源（离线/内网环境会加载失败）: {e}" for e in externals], "外链",
        ))

    # ---- 6. 图表容器高度 ----
    warnings.extend(_cap(_chart_container_height_warnings(html), "容器高度"))

    # ---- 7. 空产物兜底 ----
    # 只在"确实什么都没有"时报，且报 warning 不报 error：内容简短是合法情况
    # （落地页、跳转页），把真判定交给 Tier1 的实际渲染结果，避免假阳性引发返工。
    _visible = re.sub(r"<script\b.*?</script\s*>", " ", html, flags=re.IGNORECASE | re.DOTALL)
    _visible = re.sub(r"<style\b.*?</style\s*>", " ", _visible, flags=re.IGNORECASE | re.DOTALL)
    _visible = re.sub(r"<[^>]+>", " ", _visible).strip()
    _has_external_script = any("src=" in (attrs or "").lower() for attrs, _ in scripts)
    if "<body" in low and not _visible and not inline_js.strip() and not _has_external_script:
        warnings.append("页面无任何可见文字与脚本，疑似空产物")

    meta["script_blocks"] = len(scripts)
    meta["has_inline_js"] = bool(inline_js.strip())

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "externals": externals,
        "meta": meta,
    }
