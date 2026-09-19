"""家长/主管通知通道。

系统不做医学判断：异常处置只负责如实记录现场观察并发出通知，
通知结果以回执形式落库（状态、通道、回执号、时间）。
"""

from __future__ import annotations

import logging
import uuid
from typing import Protocol

from .db import utcnow

log = logging.getLogger("washcare.notifications")


class NotificationSender(Protocol):
    def send(self, *, channel: str, recipient: str, subject: str, content: str) -> dict:
        ...


class LogNotificationSender:
    """默认发送器：模拟短信/App 推送，返回通道回执。

    tests 中可用 FailingNotificationSender 重放通知失败，
    失败回执同样落库，主管端仍可看到"已尝试通知但失败"的状态。
    """

    def send(self, *, channel: str, recipient: str, subject: str, content: str) -> dict:
        receipt = f"{channel.upper()}-{uuid.uuid4().hex[:12]}"
        log.info("[%s] -> %s | %s | %s", channel, recipient, subject, receipt)
        return {"status": "sent", "receipt": receipt, "sent_at": utcnow()}


class FailingNotificationSender:
    """模拟通道故障的发送器，供测试重放。"""

    def send(self, *, channel: str, recipient: str, subject: str, content: str) -> dict:
        return {"status": "failed", "receipt": "", "sent_at": utcnow()}
