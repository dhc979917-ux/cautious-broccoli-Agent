from datetime import datetime

from sqlalchemy import Column, Integer, String, DateTime, Text

from app.database import Base


'''绑定邮箱的邮件处理记录：以 Message-ID 为唯一键判定"新邮件"。
status: unread=已列出未细看 / read=已读正文 / replied=已回复。
IMAP 服务器侧的 \Seen 标记不可靠（共享邮箱、PEEK 策略），以本表为准。'''


class EmailRecord(Base):
    __tablename__ = "email_records"

    id = Column(Integer, primary_key=True, index=True)
    message_id = Column(String(512), unique=True, nullable=False, index=True)
    uid = Column(String(32), nullable=True)
    sender = Column(String(255), nullable=True)
    # 回复地址：优先 Reply-To，缺省 From
    reply_to = Column(String(255), nullable=True)
    subject = Column(String(512), nullable=True)
    status = Column(String(16), nullable=False, default="unread")
    summary = Column(Text, nullable=True)
    received_at = Column(String(64), nullable=True)
    processed_at = Column(DateTime, nullable=True)
    replied_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
