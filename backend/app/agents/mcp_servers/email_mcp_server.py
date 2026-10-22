"""邮件 MCP server（stdio）：即插即用的个人邮箱查收/阅读/回复能力。

凭据来源：平台在构建 Agent 时把当前用户在"个人信息"中配置的邮箱账号/密码
通过环境变量 EMAIL_MCP_USER / EMAIL_MCP_PASS 注入本子进程（每用户独立构建、
独立子进程，天然隔离）。未配置时三个工具统一返回配置指引，不做任何降级。

注册方式：管理端 MCP 页面新增 stdio 服务，command=python 绝对路径，
args=["<backend>/app/agents/mcp_servers/email_mcp_server.py"]，
再在 Agent 配置里勾选绑定即可。
"""
import os
import sys

# 子进程自举：stdio 方式拉起时保证能 import app.* 并读到 backend 目录
_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, "..", "..", ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)
os.chdir(_BACKEND)

# MCP SDK 双版本兼容（1.x FastMCP / 2.x MCPServer）
try:
    from mcp.server.fastmcp import FastMCP as _MCP
except Exception:
    from mcp.server.mcpserver import MCPServer as _MCP  # noqa: E402

from app.core.email_core import (  # noqa: E402
    MailAccount,
    DEFAULT_MAIL_HOST,
    DEFAULT_IMAP_PORT,
    DEFAULT_SMTP_PORT,
    check_new_emails,
    read_email_by_id,
    reply_to_email,
    send_new_email,
)

_USER = os.environ.get("EMAIL_MCP_USER", "").strip()
_PASS = os.environ.get("EMAIL_MCP_PASS", "")
# 平台用户 id：读取邮件时附件归档到文件中心 uploads/；
# 由平台构建子进程时注入（见 base.py），未注入时为空、附件只报元信息
_USER_ID = os.environ.get("EMAIL_MCP_USER_ID", "").strip()

NOT_CONFIGURED = (
    "当前账号未配置邮箱，邮件功能不可用。请先到【个人信息 → 联系方式】中"
    "填写邮箱地址并保存邮箱密码（邮箱平台的独立密码/授权码），保存后重新发起对话即可使用。"
)


def _account():
    """凭据完整时构造 MailAccount，否则返回 None（由调用方统一回复 NOT_CONFIGURED）"""
    if not _USER or not _PASS:
        return None
    return MailAccount(
        user=_USER,
        password=_PASS,
        imap_host=os.environ.get("EMAIL_MCP_IMAP_HOST", "").strip() or DEFAULT_MAIL_HOST,
        imap_port=int(os.environ.get("EMAIL_MCP_IMAP_PORT", "").strip() or DEFAULT_IMAP_PORT),
        smtp_host=os.environ.get("EMAIL_MCP_SMTP_HOST", "").strip() or DEFAULT_MAIL_HOST,
        smtp_port=int(os.environ.get("EMAIL_MCP_SMTP_PORT", "").strip() or DEFAULT_SMTP_PORT),
        smtp_security=os.environ.get("EMAIL_MCP_SMTP_SECURITY", "").strip() or "auto",
    )


# 注意：AgentScope 的 MCPClient 只拉取工具、不透出 server instructions，
# 因此完整处理流程写在各工具 docstring 里（模型必然可见），此处仅留备忘。
INSTRUCTIONS = """邮件处理助手：查收→阅读→按 ABCD 四选项决策→执行/回复。
详细流程见 check_emails / read_email / reply_email 三个工具的描述。"""

mcp = _MCP("email", instructions=INSTRUCTIONS)


@mcp.tool()
def check_emails(limit: int = 20, sender: str = "", include_read: bool = False) -> str:
    """检查个人邮箱中的新邮件（默认只看最近 7 天内的"未读"邮件）。

    未读判定以邮箱服务器的已读标志为准：别人发来的未读邮件、以及自己读过
    后又标记为未读的邮件都会列出；已读的自动跳过。返回每封邮件的主题/
    发件人/时间/message_id 清单；没有新邮件时明确说明。
    没有新邮件时只回复"无新邮件"。有新邮件时：逐封调用 read_email 读取
    正文并向用户汇报（发件人/主题/2~3 句大意/其中的任务诉求），然后必须
    按 read_email 描述中的 ABCD 四选项流程用 AskUserChoice 征求处理意见，
    每一封未读邮件都要单独询问一次，不要合并或遗漏。

    【按发件人筛选】用户若指定了发件人（如"读一下 ai-notice 的邮件"），
    把该关键词填入 sender 参数，本次只处理该发件人的邮件。

    【找回已读邮件】如果你需要回复一封已读邮件、而手头没有它的
    message_id（之前读过、上下文里已丢失），就用 include_read=true 重新
    调用本工具——此时最近 7 天内的邮件（含已读）也会被列出，从中取回
    message_id，再调用 reply_email 回复。

    Args:
        limit: 最多扫描收件箱最近多少封邮件（默认 20，最大 50）。
        sender: 可选，发件人筛选关键词（如 "ai-notice"），只列匹配的邮件。
        include_read: 是否把已读邮件也列出（默认 false；仅用于找回
            已读邮件的 message_id 以便回复，不改变其已读状态）。
    """
    account = _account()
    if account is None:
        return NOT_CONFIGURED
    return check_new_emails(limit, account, sender=sender, include_read=include_read)


@mcp.tool()
def read_email(message_id: str) -> str:
    """读取一封邮件的完整正文与附件清单（message_id 来自 check_emails）。

    读取后该邮件会被标记为已读，不再作为新邮件出现。
    在总结邮件内容或执行其中要求的任务之前，先调用本工具。

    【重要】读完邮件、向用户汇报大意之后，必须调用 AskUserChoice 让用户选择
    如何处理这封邮件，固定四个选项：
    A. 仅处理——执行邮件中要求的任务，但不回复邮件；
    B. 处理后回复——执行任务，并生成回复邮件；
    C. 直接回复——不执行任务，只生成回复文本；
    D. 标记已读暂不处理——仅归档，不做任何回复或任务。
    按用户所选执行：选 A 只执行任务不回复；选 D 什么都不做，告知已归档；
    选 B/C 涉及回复时，先把完整草稿（收件人/主题/正文/附件清单）展示给用户，
    再按 reply_email 描述中的"确认发送 / 修改草稿 / 取消发送"流程确认后发送。

    邮件的附件会自动下载并转存到当前用户的文件中心
    （uploads/），同时登记进平台文件中心；返回结果里
    会给出每个附件的文件名与下载/读取入口。后续需要读取附件内容时，按
    返回的入口走平台既有的文件读取逻辑即可。

    Args:
        message_id: 邮件的 Message-ID（来自 check_emails 的返回）。
    """
    account = _account()
    if account is None:
        return NOT_CONFIGURED
    return read_email_by_id(message_id, account,
                            user_id=int(_USER_ID) if _USER_ID.isdigit() else 0)


@mcp.tool()
def reply_email(message_id: str, body: str, subject: str = "", attachments: list | str | None = None) -> str:
    """回复 check_emails 曾列出的一封邮件。

    收件人取原邮件的 Reply-To/From 地址；主题缺省为 'Re: 原主题'；
    attachments 为文件路径列表，必须是工作输出目录中真实生成的文件
    （多个文件会自动打包为一个 zip）。
    attachments 推荐传 JSON 数组（如 ["E:/.../a.html"]）；也兼容传单个路径
    字符串或 JSON 数组字符串，工具内部会自动归一化为列表。

    重要约束：发送邮件是对外副作用。调用本工具之前，必须先把完整草稿
    （收件人/主题/正文/附件清单）展示给用户，并通过 AskUserChoice 让用户在
    “确认发送 / 修改草稿 / 取消发送”中选择；仅当用户确认发送后才可调用，
    用户选择取消时不得调用。发送成功后只向用户简报一句
    “已回复 <发件人>（主题：xxx）”，不要复述草稿内容、附件清单或任务细节。
    可回复范围不设限：只要是 check_emails（含 include_read）曾列出、或用户
    明确给出 message_id 的邮件，都可以回复，不要求邮件必须是未读。
    本工具只在用户于 ABCD 决策中选择了 B（处理后回复）或 C（直接回复）、
    且在随后的发送确认中选择"确认发送"时才允许调用；重复回复同一封邮件时
    工具会给出提醒，请先向用户确认。

    Args:
        message_id: 要回复的邮件 Message-ID（来自 check_emails）。
        body: 回复正文（纯文本）。
        subject: 回复主题，缺省为 Re: 原主题。
        attachments: 附件文件路径（数组，或单个路径/JSON 数组字符串；
            必须位于工作输出目录内；多个自动打包 zip）。
    """
    account = _account()
    if account is None:
        return NOT_CONFIGURED
    # 归一化：模型可能把数组序列化成字符串传进来，统一转成 list 再下发
    from app.core.email_core import _normalize_attachments
    attachments = _normalize_attachments(attachments) or None
    return reply_to_email(message_id, body, subject, attachments, account)


@mcp.tool()
def send_email(to_addr: str, subject: str, body: str, attachments: list | str | None = None) -> str:
    """主动发送一封全新邮件给指定收件人（非回复已有邮件）。
    收件人地址由参数 to_addr 直接指定，不依赖 check_emails 的 message_id。
    适用于：用户要求主动通知某人、发送报告/交付物、催办等场景。

    重要约束（与 reply_email 相同）：
    调用本工具之前，必须先把完整草稿（收件人/主题/正文/附件清单）展示给用户，
    并通过 AskUserChoice 在确认发送/修改草稿/取消发送中征求确认；
    仅当用户选择确认发送后才可调用。用户选择取消时不调用。
    发送成功后只简报一句已发送给收件人（主题：xxx），
    不要复述草稿内容、附件清单或任务细节。

    attachments 为文件路径数组，必须是工作输出目录中真实存在的文件
    （多个文件会自动打包为一个 zip）。推荐传 JSON 数组（如 ["E:/.../a.html"]）；
    也兼容传单个路径字符串或 JSON 数组字符串，工具内部会自动归一化为列表。

    Args:
        to_addr: 收件人邮箱地址。
        subject: 邮件主题。
        body: 邮件正文（纯文本）。
        attachments: 附件文件路径（数组，或单个路径 / JSON 数组字符串；
            必须位于工作输出目录内；多个自动打包 zip）。
    """
    account = _account()
    if account is None:
        return NOT_CONFIGURED
    from app.core.email_core import _normalize_attachments
    attachments = _normalize_attachments(attachments) or None
    return send_new_email(to_addr, subject, body, attachments, account)




@mcp.tool()
def lookup_user_email(name: str) -> str:
    """按姓名或小奞账号查询平台用户的邮箱地址。
    用于发邮件时用户只给了姓名/账号而未给邮箱地址的场景。
    查到且已配置邮箱时返回邮箱地址；查不到或未配置时返回提示，由 AI 向用户索要邮箱地址。

    Args:
        name: 用户名或账号（如 "张三" 或 "zhangsan"）。
    """
    from app.database import SessionLocal
    from app.models.user import User
    from sqlalchemy import func, or_

    name = (name or "").strip()
    if not name:
        return "请提供用户姓名或账号。"

    db = SessionLocal()
    try:
        u = db.query(User).filter(func.lower(User.account) == name.lower()).first()
        if not u:
            u = db.query(User).filter(User.username == name).first()
        if not u:
            like = f"%{name}%"
            rows = (db.query(User)
                    .filter(User.is_active == True,  # noqa: E712
                            or_(User.username.like(like), User.account.like(like)))
                    .all())
            if len(rows) == 1:
                u = rows[0]
            elif len(rows) > 1:
                cands = [f"{r.username}(账号:{r.account or '无'})" for r in rows[:8]]
                return ("匹配到多个用户，请让用户确认具体是哪位：\n- "
                        + "\n- ".join(cands)
                        + "\n确认后再次调用本工具，或直接让用户提供邮箱地址。")
        if not u:
            return (f"未找到平台用户“{name}”。请向用户询问对方的邮箱地址。")
        if not u.email or not u.email.strip():
            return (f"用户“{u.username}”（账号:{u.account or '无'}）尚未配置邮箱地址。"
                    "请向用户询问对方的邮箱地址。")
        return f"{u.username} 的邮箱地址是 {u.email.strip()}"
    finally:
        db.close()


if __name__ == "__main__":
    mcp.run()
