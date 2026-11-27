# -*- coding: utf-8 -*-
"""
邮件统计采集器

支持：
- 网易邮箱 (163/126)
- 通过 IMAP 协议读取邮件

功能：
- 统计今日收发邮件数量
- 获取未读邮件
- 提取重要邮件摘要
"""

import email
from dataclasses import dataclass, field
from datetime import datetime
from email.header import decode_header
from typing import Optional
from zoneinfo import ZoneInfo

_REPORT_TZ = ZoneInfo("Asia/Shanghai")

try:
    import imaplib

    IMAP_AVAILABLE = True
except ImportError:
    IMAP_AVAILABLE = False
    imaplib = None


# 网易邮箱 IMAP 服务器配置
NETEASE_IMAP_SERVERS = {
    "163": "imap.163.com",
    "126": "imap.126.com",
    "yeah": "imap.yeah.net",
}


@dataclass
class EmailInfo:
    """邮件信息"""

    subject: str  # 主题
    sender: str  # 发件人
    date: datetime  # 日期
    is_read: bool = False  # 是否已读
    is_starred: bool = False  # 是否星标

    def to_dict(self) -> dict:
        return {
            "subject": self.subject,
            "sender": self.sender,
            "date": self.date.isoformat(),
            "is_read": self.is_read,
            "is_starred": self.is_starred,
        }


@dataclass
class EmailStats:
    """邮件统计数据"""

    received_today: int = 0  # 今日收件数
    sent_today: int = 0  # 今日发件数
    unread: int = 0  # 未读邮件数
    starred: int = 0  # 星标邮件数
    important_emails: list[EmailInfo] = field(default_factory=list)  # 重要邮件

    def to_dict(self) -> dict:
        return {
            "received_today": self.received_today,
            "sent_today": self.sent_today,
            "unread": self.unread,
            "starred": self.starred,
            "important_emails": [e.to_dict() for e in self.important_emails],
        }


class EmailCollector:
    """邮件统计采集器"""

    def __init__(
        self,
        email_address: str,
        auth_code: str,
        provider: str = "163",
    ):
        """
        初始化邮件采集器

        Args:
            email_address: 邮箱地址
            auth_code: 授权码（不是登录密码）
            provider: 邮箱提供商 (163/126/yeah)
        """
        if not IMAP_AVAILABLE:
            raise ImportError("imaplib 模块不可用")

        self.email_address = email_address
        self.auth_code = auth_code
        self.provider = provider.lower()

        if self.provider not in NETEASE_IMAP_SERVERS:
            raise ValueError(f"不支持的邮箱提供商: {provider}")

        self.imap_server = NETEASE_IMAP_SERVERS[self.provider]
        self._connection = None

    @staticmethod
    def _decode_str(s: str) -> str:
        """解码邮件字符串"""
        if s is None:
            return ""

        decoded_parts = decode_header(s)
        result = []

        for part, encoding in decoded_parts:
            if isinstance(part, bytes):
                try:
                    result.append(part.decode(encoding or "utf-8", errors="replace"))
                except Exception:
                    result.append(part.decode("utf-8", errors="replace"))
            else:
                result.append(part)

        return "".join(result)

    @staticmethod
    def _parse_date(date_str: str) -> Optional[datetime]:
        """解析邮件日期"""
        if not date_str:
            return None

        try:
            # 使用 email.utils 解析日期
            from email.utils import parsedate_to_datetime

            return parsedate_to_datetime(date_str)
        except Exception:
            return None

    def connect(self) -> bool:
        """
        连接 IMAP 服务器

        Returns:
            是否连接成功
        """
        try:
            self._connection = imaplib.IMAP4_SSL(self.imap_server, 993)
            self._connection.login(self.email_address, self.auth_code)
            return True
        except Exception as e:
            print(f"连接邮箱失败: {e}")
            return False

    def disconnect(self):
        """断开连接"""
        if self._connection:
            try:
                self._connection.logout()
            except Exception:
                pass
            self._connection = None

    def get_stats(self, date: Optional[str] = None) -> EmailStats:
        """
        获取邮件统计

        Args:
            date: 日期字符串 (YYYY-MM-DD)，默认今天

        Returns:
            EmailStats: 邮件统计数据
        """
        if date is None:
            date = datetime.now(_REPORT_TZ).strftime("%Y-%m-%d")

        stats = EmailStats()

        if not self._connection:
            if not self.connect():
                return stats

        try:
            # 选择收件箱
            self._connection.select("INBOX")

            # 搜索今日邮件
            date_obj = datetime.strptime(date, "%Y-%m-%d")
            imap_date = date_obj.strftime("%d-%b-%Y")

            # 获取今日收到的邮件
            status, messages = self._connection.search(
                None, f'(ON "{imap_date}")'
            )

            if status == "OK" and messages[0]:
                today_message_ids = messages[0].split()
                stats.received_today = len(today_message_ids)

            # 获取未读邮件
            status, messages = self._connection.search(None, "UNSEEN")
            if status == "OK" and messages[0]:
                unseen_ids = messages[0].split()
                stats.unread = len(unseen_ids)

                # 获取未读邮件详情（最多5封）
                for msg_id in unseen_ids[:5]:
                    email_info = self._get_email_info(msg_id)
                    if email_info:
                        stats.important_emails.append(email_info)

            # 获取星标邮件
            status, messages = self._connection.search(None, "FLAGGED")
            if status == "OK" and messages[0]:
                stats.starred = len(messages[0].split())

            # 尝试获取发件箱统计
            try:
                status, _ = self._connection.select("Sent")
                if status == "OK":
                    status, messages = self._connection.search(
                        None, f'(ON "{imap_date}")'
                    )
                    if status == "OK" and messages[0]:
                        stats.sent_today = len(messages[0].split())
            except Exception:
                pass  # 发件箱可能不存在或无权限

        except Exception as e:
            print(f"获取邮件统计失败: {e}")

        return stats

    def _get_email_info(self, msg_id: bytes) -> Optional[EmailInfo]:
        """获取邮件详情"""
        try:
            status, msg_data = self._connection.fetch(msg_id, "(RFC822)")
            if status != "OK":
                return None

            raw_email = msg_data[0][1]
            msg = email.message_from_bytes(raw_email)

            subject = self._decode_str(msg.get("Subject", ""))
            sender = self._decode_str(msg.get("From", ""))
            date = self._parse_date(msg.get("Date", ""))

            # 检查是否已读
            flags = ""
            if len(msg_data) > 1:
                flags = str(msg_data[0][0], errors="replace")

            is_read = "\\Seen" in flags or "seen" in flags.lower()

            return EmailInfo(
                subject=subject,
                sender=sender,
                date=date or datetime.now(_REPORT_TZ),
                is_read=is_read,
            )

        except Exception:
            return None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.disconnect()
        return False


def main():
    """测试入口"""
    import os
    import sys

    if len(sys.argv) < 3:
        print("Usage: python email_collector.py <email> <auth_code> [provider]")
        sys.exit(1)

    email_address = sys.argv[1]
    auth_code = sys.argv[2]
    provider = sys.argv[3] if len(sys.argv) > 3 else "163"

    print(f"连接 {provider} 邮箱: {email_address}")

    try:
        with EmailCollector(email_address, auth_code, provider) as collector:
            stats = collector.get_stats()

            print(f"\n邮件统计:")
            print(f"  今日收件: {stats.received_today} 封")
            print(f"  今日发件: {stats.sent_today} 封")
            print(f"  未读邮件: {stats.unread} 封")
            print(f"  星标邮件: {stats.starred} 封")

            if stats.important_emails:
                print("\n未读邮件:")
                for email_info in stats.important_emails:
                    print(f"  - [{email_info.sender}] {email_info.subject}")

    except Exception as e:
        print(f"错误: {e}")


if __name__ == "__main__":
    main()
