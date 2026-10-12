"""产物验证服务（做完再看）。

分层设计，由便宜到贵、逐层短路：
- Tier 0  静态检查：纯 Python 标准库，无浏览器，必跑。抓 CSS 变量未定义、
          id 引用悬空、外链 CDN、脚本语法错误这类"白屏级"缺陷。
- Tier 1  无头渲染：node + Playwright，只回传文本结论（console/pageerror/
          canvas 尺寸/溢出），不回传截图。
- Tier 2  视觉抽查：最多 1 张缩略截图，仅在 Tier 1 报 layout_risk 或显式要求时。

对外只暴露 verify()；分层实现拆在同目录的 static_check / render_check 里，
新增一层或一种检查项不影响调用方。
"""

from app.services.artifact_verify.service import VerifyVerdict, verify

__all__ = ["VerifyVerdict", "verify"]
