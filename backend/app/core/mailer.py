import logging
import os
import smtplib
import tempfile
import zipfile
from email import encoders
from email.header import Header
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, parseaddr

from app.config import settings

logger = logging.getLogger("mailer")


'''邮件发送。两类调用方：
1) 系统邮件（忘记密码验证码等）：不传 account，用 .env 的 SMTP_* 系统配置；
   SMTP_HOST 为空时走 mock（整封打进后端日志），便于内网联调。
2) Agent 邮件 MCP：传显式 account（用户在个人信息里配置的邮箱+密码），
   永远真实发送，不做 mock、不降级到系统配置。
附件多于一个时自动打包成单个 zip。'''

MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024  # 单封附件总量上限 25MB


def _zip_files(paths: list) -> str:
    """把多个文件打包为一个临时 zip，返回 zip 路径（调用方负责删除）。"""
    fd, zip_path = tempfile.mkstemp(suffix=".zip", prefix="mail_attach_")
    os.close(fd)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in paths:
            zf.write(p, arcname=os.path.basename(p))
    return zip_path


def _attach(msg: MIMEMultipart, path: str):
    with open(path, "rb") as f:
        part = MIMEBase("application", "octet-stream")
        part.set_payload(f.read())
    encoders.encode_base64(part)
    # 中文文件名走 RFC2231，避免乱码
    filename = os.path.basename(path)
    part.add_header("Content-Disposition", "attachment", filename=("utf-8", "", filename))
    msg.attach(part)


def send_mail(to_addr: str, subject: str, body: str, attachments=None, account=None) -> bool:
    """发送邮件（可带附件）。成功返回 True，失败返回 False。

    account: dict，显式凭据 {user, password, host, port, security, from_addr}，
    用于邮件 MCP（用户个人邮箱）。不传则用 .env 系统 SMTP 配置（系统邮件）。
    """
    acc = account or {}
    host = acc.get("host") or settings.SMTP_HOST
    port = acc.get("port") or settings.SMTP_PORT or 25
    security = (acc.get("security") or settings.SMTP_SECURITY or "auto").lower()
    smtp_user = acc.get("user") or settings.SMTP_USER
    smtp_pass = acc.get("password") if acc.get("password") is not None else settings.SMTP_PASS
    from_addr = acc.get("from_addr") or settings.SMTP_FROM or smtp_user

    files = []
    for p in (attachments or []):
        if p and os.path.isfile(p):
            files.append(p)

    total = sum(os.path.getsize(f) for f in files)
    if total > MAX_ATTACHMENT_BYTES:
        logger.error("[MAILER] attachments too large: %s bytes", total)
        return False

    zip_path = None
    if len(files) > 1:
        zip_path = _zip_files(files)
        files = [zip_path]

    # mock 仅限系统邮件路径（未显式传凭据且未配置 SMTP_HOST）
    if not account and not settings.SMTP_HOST:
        logger.info(
            "[MAILER][MOCK] to=%s subject=%s attachments=%s body=%s",
            to_addr, subject, [os.path.basename(f) for f in files], body,
        )
        if zip_path:
            try: os.remove(zip_path)
            except OSError: pass
        return True

    if not host or not smtp_user:
        logger.error("[MAILER] 无可用 SMTP 凭据（account 与 .env 均未提供）")
        return False

    if files:
        msg = MIMEMultipart()
        msg.attach(MIMEText(body, "plain", "utf-8"))
        for f in files:
            _attach(msg, f)
    else:
        msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    # Coremail 要求 From 地址 == 登录账号；只调整系统邮件的展示名称。
    if account:
        msg["From"] = from_addr
    else:
        msg["From"] = formataddr((str(Header("SparkWork", "utf-8")), from_addr))
    msg["To"] = to_addr

    try:
        if security == "ssl":
            server = smtplib.SMTP_SSL(host, int(port), timeout=15)
        else:
            server = smtplib.SMTP(host, int(port), timeout=15)
            server.ehlo()
            if security == "starttls" or (security == "auto" and server.has_extn("starttls")):
                server.starttls()
                server.ehlo()
        try:
            server.login(smtp_user, smtp_pass or "")
            server.sendmail(parseaddr(from_addr)[1], [to_addr], msg.as_string())
        finally:
            try:
                server.quit()
            except Exception:
                pass
        return True
    except Exception as e:
        logger.exception("[MAILER] send failed: to=%s err=%s", to_addr, e)
        return False
    finally:
        if zip_path:
            try: os.remove(zip_path)
            except OSError: pass
