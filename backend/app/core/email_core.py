"""邮件功能公共层：IMAP 查收、邮件解析、email_records 状态、回复发送。

由邮件 MCP 服务（app/agents/mcp_servers/email_mcp_server.py）调用。
凭据完全显式（MailAccount，来自用户在个人信息中配置的邮箱+密码），
不读全局邮箱配置、不做任何降级；未配置时由 MCP 层直接提示用户去配置。

处理状态以 email_records 表为准（Message-ID 唯一键）：
- 不在表中 = 新邮件，列出时登记 status=unread；
- read = 已读正文；replied = 已回复；
- check_new_emails 只返回未处理邮件（read/replied 一律跳过），
  实现"看过/回过的不再算新"。
回复地址优先 Reply-To，缺省 From；附件路径必须位于 OUTPUT_DIR 之内。"""
import email as emaillib
import imaplib
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.header import decode_header
from email.utils import parseaddr
from typing import List, Optional

from app.config import OUTPUT_DIR

BODY_LIMIT = 3000      # 正文截断上限（保护上下文）
SUMMARY_LIMIT = 200    # 落库摘要长度
FETCH_LIMIT_MAX = 50   # 单次最多扫描最近多少封
UNSEEN_WINDOW_DAYS = 7  # check_new_emails 的"未读邮件"时间窗口（天）

DEFAULT_MAIL_HOST = ""  # 留空：未配置 SMTP/IMAP 主机时按用户邮箱域名推导（mail.<domain>）
DEFAULT_IMAP_PORT = 143
DEFAULT_SMTP_PORT = 25


@dataclass
class MailAccount:
    """收发信凭据（用户个人邮箱）。from 固定等于登录账号（Coremail 要求）。"""
    user: str
    password: str
    imap_host: str = DEFAULT_MAIL_HOST
    imap_port: int = DEFAULT_IMAP_PORT
    smtp_host: str = DEFAULT_MAIL_HOST
    smtp_port: int = DEFAULT_SMTP_PORT
    smtp_security: str = "auto"

    @property
    def from_addr(self) -> str:
        return self.user

    def as_smtp_account(self) -> dict:
        return {
            "user": self.user,
            "password": self.password,
            "host": self.smtp_host,
            "port": self.smtp_port,
            "security": self.smtp_security,
            "from_addr": self.from_addr,
        }


# ===== IMAP / 解析辅助 =====


def _imap_connect(account: MailAccount) -> imaplib.IMAP4:
    if not account.user or not account.password:
        raise RuntimeError("缺少邮箱账号或密码")
    conn = imaplib.IMAP4(account.imap_host, int(account.imap_port), timeout=20)
    conn.login(account.user, account.password)
    return conn


def _decode_header_value(raw) -> str:
    """RFC2047 头解码，charset/UTF-8/GB18030 逐级兜底（国内邮件常见 GBK）"""
    if not raw:
        return ""
    parts = []
    for chunk, charset in decode_header(str(raw)):
        if isinstance(chunk, bytes):
            text = None
            for enc in (charset, "utf-8", "gb18030"):
                if not enc:
                    continue
                try:
                    text = chunk.decode(enc)
                    break
                except (LookupError, UnicodeDecodeError):
                    continue
            parts.append(text if text is not None else chunk.decode("utf-8", errors="replace"))
        else:
            parts.append(chunk)
    return "".join(parts).strip()


def _decode_payload(part) -> str:
    payload = part.get_payload(decode=True)
    if not payload:
        return ""
    charset = part.get_content_charset()
    for enc in (charset, "utf-8", "gb18030"):
        if not enc:
            continue
        try:
            return payload.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return payload.decode("utf-8", errors="replace")


def _extract(msg) -> tuple:
    """返回 (正文, 附件元信息列表)。text/plain 优先，缺省取 HTML 去标签。"""
    body, html_body = "", ""
    attachments = []
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        disp = str(part.get("Content-Disposition") or "").lower()
        name = part.get_filename()
        if name or "attachment" in disp:
            size = len(part.get_payload(decode=True) or b"")
            attachments.append({"name": _decode_header_value(name) if name else "未命名附件", "size": size})
            continue
        ctype = part.get_content_type()
        text = _decode_payload(part)
        if not text:
            continue
        if ctype == "text/plain" and not body:
            body = text
        elif ctype == "text/html" and not html_body:
            html_body = text
    if not body and html_body:
        body = re.sub(r"(?is)<(script|style).*?>.*?</\1>", "", html_body)
        body = re.sub(r"(?s)<br\s*/?>", "\n", body)
        body = re.sub(r"(?s)<[^>]+>", " ", body)
        body = re.sub(r"[ \t]+", " ", body)
        body = re.sub(r"\n\s*\n+", "\n\n", body)
    return body.strip(), attachments


def _extract_attachment_payloads(msg) -> List[dict]:
    """提取邮件附件的字节内容：[{name, data, content_type}]。

    与 _extract 的附件元信息判定规则保持一致（有文件名或 disposition 为
    attachment 即视为附件）。"""
    out = []
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        disp = str(part.get("Content-Disposition") or "").lower()
        name = part.get_filename()
        if not (name or "attachment" in disp):
            continue
        out.append({
            "name": _decode_header_value(name) if name else "未命名附件",
            "data": part.get_payload(decode=True) or b"",
            "content_type": part.get_content_type(),
        })
    return out


def _store_attachments_to_file_center(user_id: int, attachments: List[dict]) -> tuple:
    """把邮件附件保存到本地文件中心（uploads/）并登记 FileRecord。

    返回 (成功引用列表, 失败说明列表)；单个附件失败不影响其余附件。
    低耦合：依赖全部惰性导入，文件服务不可用时整体降级为失败说明，
    不抛异常、不阻断邮件正文读取。"""
    refs, errors = [], []
    try:
        from app.config import UPLOAD_DIR
        from app.database import SessionLocal
        from app.services.file_service import FileService
        os.makedirs(str(UPLOAD_DIR), exist_ok=True)
    except Exception as e:
        return [], [f"文件中心不可用，附件未保存: {e}"]
    db = SessionLocal()
    try:
        fs = FileService(db)
        ts = datetime.now().strftime("%Y%m%d%H%M%S")
        for att in attachments:
            name = att.get("name") or "附件"
            data = att.get("data") or b""
            try:
                stored_name = f"{int(user_id)}_{ts}_{name}"
                with open(os.path.join(str(UPLOAD_DIR), stored_name), "wb") as fh:
                    fh.write(data)
                rec = fs.register_upload(
                    user_id=int(user_id), stored_name=stored_name,
                    original_name=name, size=len(data),
                    content_type=att.get("content_type"),
                )
                refs.append({"name": name, "size": len(data), "file_id": rec.id})
            except Exception as e:
                errors.append(f"{name}: {e}")
    finally:
        db.close()
    return refs, errors


def _parse_header_block(raw: bytes) -> dict:
    msg = emaillib.message_from_bytes(raw)
    message_id = (msg.get("Message-ID") or "").strip()
    from_raw = _decode_header_value(msg.get("From"))
    reply_raw = _decode_header_value(msg.get("Reply-To"))
    reply_addr = parseaddr(reply_raw)[1] or parseaddr(from_raw)[1]
    return {
        "message_id": message_id,
        "from": from_raw,
        "reply_to": reply_addr,
        "subject": _decode_header_value(msg.get("Subject")),
        "date": (msg.get("Date") or "").strip(),
    }


def _normalize_attachments(attachments) -> List[str]:
    """容错模型传附件的各种形态：list / JSON 数组字符串 / 单个路径字符串。"""
    if attachments is None:
        return []
    if isinstance(attachments, str):
        s = attachments.strip()
        if not s:
            return []
        if s.startswith("["):
            try:
                import json as _json
                parsed = _json.loads(s)
                if isinstance(parsed, list):
                    return [str(x).strip() for x in parsed if str(x).strip()]
            except Exception:
                pass
        return [s]
    if isinstance(attachments, (list, tuple)):
        return [str(x).strip() for x in attachments if str(x).strip()]
    return []


def _check_attachment_paths(paths: List[str]) -> List[str]:
    """附件路径校验：必须存在且位于 OUTPUT_DIR 之内（防止把任意文件发出去）。
    相对路径直接解析不到时（模型常漏掉 user_X 一层），在 OUTPUT_DIR 内按后缀查找。"""
    base = os.path.realpath(str(OUTPUT_DIR))
    resolved = []
    for p in paths or []:
        if not p:
            continue
        ap = p if os.path.isabs(p) else os.path.join(base, p)
        rp = os.path.realpath(ap)
        if rp.startswith(base + os.sep) and os.path.isfile(rp):
            resolved.append(rp)
            continue
        if not os.path.isabs(p):
            needle = p.replace("\\", "/").strip("/").lower()
            found = None
            for root, _dirs, files in os.walk(base):
                for fn in files:
                    cand = os.path.realpath(os.path.join(root, fn))
                    rel = os.path.relpath(cand, base).replace("\\", "/").lower()
                    if rel == needle or rel.endswith("/" + needle):
                        found = cand
                        break
                if found:
                    break
            if found:
                resolved.append(found)
                continue
        if not rp.startswith(base + os.sep):
            raise ValueError(f"附件路径不在允许范围内：{p}")
        raise ValueError(f"附件不存在：{p}（必须是 outputs 目录内真实生成的文件）")
    return resolved


# ===== 核心函数 =====


def check_new_emails(limit: int, account: MailAccount, sender: str = "",
                     include_read: bool = False) -> str:
    """列出邮箱中的未读邮件（最近 UNSEEN_WINDOW_DAYS 天，IMAP 未读标志为准）。

    已读后又被标记为未读的邮件会重新出现（DB 状态随之复位）；已读邮件默认不列。
    sender 非空时只列发件人含该关键词的邮件（IMAP FROM + 本地包含双重过滤）。
    include_read=True 时范围放宽为窗口内全部邮件（用于找回已读邮件的
    message_id 以便回复），输出中带 [已读] 标记。"""
    from app.database import SessionLocal
    from app.models.email_record import EmailRecord

    limit = max(1, min(int(limit or 20), FETCH_LIMIT_MAX))
    sender = (sender or "").strip()
    try:
        conn = _imap_connect(account)
    except Exception as e:
        return f"邮箱连接失败：{e}（请检查个人信息中的邮箱账号与密码是否正确）"

    since = (datetime.now() - timedelta(days=UNSEEN_WINDOW_DAYS)).strftime("%d-%b-%Y")
    criteria = ["SINCE", since]
    # IMAP FROM 只对纯 ASCII 关键词生效：中文等非 ASCII 会在 imaplib/服务器侧触发
    # 编码错误，此时完全依赖下方对已解码 From 头的本地包含过滤。
    if sender and sender.isascii():
        criteria += ["FROM", sender]
    if not include_read:
        criteria.insert(0, "UNSEEN")

    try:
        conn.select("INBOX", readonly=True)
        typ, data = conn.search(None, *criteria)
        uids = data[0].split() if typ == "OK" and data and data[0] else []
        uids = uids[-limit:]

        db = SessionLocal()
        try:
            pending, skipped = [], 0
            for uid in uids:
                typ, seg = conn.fetch(uid, "(FLAGS BODY.PEEK[HEADER])")
                raw, flags = None, ""
                for item in seg or []:
                    if isinstance(item, tuple):
                        raw = item[1]
                    elif isinstance(item, bytes):
                        flags = item.decode("utf-8", "ignore")
                if not raw:
                    continue
                h = _parse_header_block(raw)
                # 客户端侧发件人过滤兜底（IMAP FROM 匹配规则各服务器不完全一致）
                if sender and sender.lower() not in (h["from"] or "").lower():
                    skipped += 1
                    continue
                seen = "\\seen" in flags.lower()
                mid = h["message_id"] or f"uid-{uid.decode()}"
                rec = db.query(EmailRecord).filter(EmailRecord.message_id == mid).first()
                if rec:
                    # IMAP 未读标志为准：已读后又标未读 → 复位为 unread 重新参与处理
                    if not seen and rec.status != "unread":
                        rec.status = "unread"
                        db.commit()
                else:
                    rec = EmailRecord(
                        message_id=mid,
                        uid=uid.decode(),
                        sender=h["from"],
                        reply_to=h["reply_to"],
                        subject=h["subject"],
                        status="read" if seen else "unread",
                        received_at=h["date"],
                    )
                    db.add(rec)
                    db.commit()
                if seen and not include_read:
                    skipped += 1
                    continue
                pending.append({
                    "message_id": mid,
                    "subject": h["subject"] or "(无主题)",
                    "from": h["from"],
                    "date": h["date"],
                    "unread": not seen,
                })
        finally:
            db.close()
    finally:
        try:
            conn.logout()
        except Exception:
            pass

    scope = f"最近 {UNSEEN_WINDOW_DAYS} 天" + (f"、发件人含「{sender}」" if sender else "")
    if not pending:
        return f"无未读新邮件（{scope}，已过滤 {skipped} 封）。"

    kind = "邮件" if include_read else "未读新邮件"
    lines = [f"共 {len(pending)} 封{kind}（{scope}，另有 {skipped} 封被过滤）："]
    for i, p in enumerate(pending, 1):
        mark = "" if p["unread"] else "[已读] "
        lines.append(
            f"【{i}】{mark}主题：{p['subject']}\n    发件人：{p['from']}\n    时间：{p['date']}\n    message_id：{p['message_id']}"
        )
    lines.append("处理前先调用 read_email(message_id=...) 查看正文；需要回复时使用 reply_email。")
    return "\n".join(lines)


def read_email_by_id(message_id: str, account: MailAccount, user_id: int = 0) -> str:
    """读取指定邮件正文与附件，置为已读、落摘要；附件按需转存文件中心。

    user_id 非 0 时，附件下载后保存到本地 uploads/
    并登记 FileRecord，返回结果给出文件名与读取/下载入口。"""
    from app.database import SessionLocal
    from app.models.email_record import EmailRecord

    message_id = (message_id or "").strip()
    if not message_id:
        return "缺少 message_id 参数。"

    db = SessionLocal()
    try:
        rec = db.query(EmailRecord).filter(EmailRecord.message_id == message_id).first()
    finally:
        db.close()
    if not rec:
        return "未找到该邮件记录，请先调用 check_emails 获取邮件列表。"

    try:
        conn = _imap_connect(account)
    except Exception as e:
        return f"邮箱连接失败：{e}（请检查个人信息中的邮箱账号与密码是否正确）"

    try:
        conn.select("INBOX")
        uid_used = None
        if rec.uid:
            uid_used = rec.uid.encode()
            typ, seg = conn.fetch(uid_used, "(RFC822)")
        else:
            typ, found = conn.search(None, "HEADER", "Message-ID", message_id)
            target = found[0].split()[-1:] if found and found[0] else []
            if target:
                uid_used = target[0]
                typ, seg = conn.fetch(uid_used, "(RFC822)")
            else:
                typ, seg = ("NO", None)
        raw = seg[0][1] if seg and isinstance(seg[0], tuple) else None
        if not raw:
            return "邮件正文获取失败（可能已被删除）。"
        # 显式标记已读：IMAP \Seen 是"未读"判定的权威标志，不依赖 fetch 的隐式效果
        if uid_used:
            try:
                conn.store(uid_used, "+FLAGS", "\\Seen")
            except Exception:
                pass
        msg = emaillib.message_from_bytes(raw)
        body, attachments = _extract(msg)
    finally:
        try:
            conn.logout()
        except Exception:
            pass

    # 置已读 + 落摘要
    db = SessionLocal()
    try:
        row = db.query(EmailRecord).filter(EmailRecord.message_id == message_id).first()
        if row and row.status == "unread":
            row.status = "read"
        if row:
            row.processed_at = datetime.utcnow()
            row.summary = body[:SUMMARY_LIMIT]
        db.commit()
    finally:
        db.close()

    truncated = len(body) > BODY_LIMIT
    text = (
        f"主题：{rec.subject}\n发件人：{rec.sender}\n时间：{rec.received_at}\n\n"
        f"正文{'（已截断至 %d 字符）' % BODY_LIMIT if truncated else ''}：\n{body[:BODY_LIMIT]}"
    )
    if attachments:
        att_lines = "、".join(f"{a['name']}({a['size']}B)" for a in attachments)
        if user_id:
            refs, errs = _store_attachments_to_file_center(int(user_id), _extract_attachment_payloads(msg))
            if refs:
                ref_lines = "；".join(
                    f"{r['name']}（file_id={r['file_id']}，读取/下载: /api/v1/files/{r['file_id']}/download）"
                    for r in refs
                )
                text += f"\n\n附件 {len(refs)} 个，已保存到文件中心（uploads/）：{ref_lines}"
            if errs:
                text += f"\n部分附件保存失败：{'；'.join(errs)}"
            if not refs and not errs:
                text += f"\n\n附件 {len(attachments)} 个：{att_lines}"
        else:
            text += f"\n\n附件 {len(attachments)} 个：{att_lines}（当前进程无用户身份，附件未转存）"
    return text


def reply_to_email(message_id: str, body: str, subject: str, attachments: Optional[List[str]], account: MailAccount) -> str:
    """回复指定邮件（附件校验、多附件自动 zip），成功后置 replied。"""
    from app.database import SessionLocal
    from app.models.email_record import EmailRecord
    from app.core.mailer import send_mail

    message_id = (message_id or "").strip()
    db = SessionLocal()
    try:
        rec = db.query(EmailRecord).filter(EmailRecord.message_id == message_id).first()
    finally:
        db.close()
    if not rec:
        return "未找到该邮件记录，请先调用 check_emails。"

    to_addr = rec.reply_to or parseaddr(rec.sender or "")[1]
    if not to_addr:
        return "该邮件没有可用的回复地址。"
    # 回复范围不设限：已回复过的邮件也允许再次回复（用户明确要求时）；
    # 保留"已回复"标记，在发送结果中附带提醒，防止模型无意中重复发送
    already_replied = rec.status == "replied"

    try:
        files = _check_attachment_paths(_normalize_attachments(attachments))
    except ValueError as e:
        return f"附件校验失败：{e}"

    final_subject = (subject or "").strip()
    if not final_subject:
        orig = rec.subject or ""
        final_subject = orig if orig.lower().startswith("re:") else (f"Re: {orig}" if orig else "Re:")

    ok = send_mail(to_addr, final_subject, body or "", files, account=account.as_smtp_account())
    if not ok:
        return f"发送失败：请检查邮箱账号/密码或稍后重试。收件人 {to_addr}，主题「{final_subject}」。请如实告知用户发送未成功。"

    db = SessionLocal()
    try:
        row = db.query(EmailRecord).filter(EmailRecord.message_id == message_id).first()
        if row:
            row.status = "replied"
            row.replied_at = datetime.utcnow()
        db.commit()
    finally:
        db.close()

    att_desc = f"，附件 {len(files)} 个（{'已打包 zip' if len(files) > 1 else os.path.basename(files[0])}）" if files else ""
    note = " 注意：此邮件此前已回复过，本次为重复回复，请向用户说明。" if already_replied else ""
    return f"回复成功：收件人 {to_addr}，主题「{final_subject}」{att_desc}。该邮件已标记为已回复。{note}"
def send_new_email(to_addr: str, subject: str, body: str,
                   attachments: Optional[List[str]] = None,
                   account: MailAccount = None) -> str:
    """主动发一封全新邮件（非回复），收件人由参数指定，不依赖任何已有邮件记录。
    凭据 account：用户个人邮箱凭据，由邮件 MCP 服务调用（或其他调用方）。
    附件校验与回复邮件相同（路径必须位于 OUTPUT_DIR 内，多个自动打包 zip）。
    成功后不向 email_records 表产生记录（有人在等回复，独立于已有邮件的状态跟踪）。
    """
    from app.core.mailer import send_mail

    to_addr = (to_addr or "").strip()
    if not to_addr:
        return "缺少收件人地址。"
    if not subject or not subject.strip():
        return "缺少邮件主题，无法发送。"
    if not body or not body.strip():
        return "邮件正文为空，无法发送。"

    try:
        files = _check_attachment_paths(_normalize_attachments(attachments))
    except ValueError as e:
        return f"附件校验失败：{e}"

    ok = send_mail(to_addr, subject.strip(), body, files,
                   account=account.as_smtp_account() if account else None)
    if not ok:
        return (f"发送失败：请检查邮箱账号/密码或稍后重试。收件人 {to_addr}，主题「{subject.strip()}」。"
                "如实告知用户发送未成功。")

    att_desc = f"，附件 {len(files)} 个（{'已打包 zip' if len(files) > 1 else os.path.basename(files[0])}）" if files else ""
    return f"发送成功：收件人 {to_addr}，主题「{subject.strip()}」{att_desc}。"
