# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""SMTP 发信。注册验证码走这里。"""
from __future__ import annotations

import smtplib
from email.message import EmailMessage
from typing import Any

from script.log import SLog

from server.services.system_settings_service import get_mail_credentials, get_mail_settings

TAG = "Mail"


def mail_ready() -> bool:
    return bool(get_mail_settings().get("configured"))


def send_mail(*, to: str, subject: str, body: str) -> None:
    cfg = get_mail_credentials()
    if not cfg.get("configured"):
        raise RuntimeError("还没有配置发信邮箱。到设置 → 密钥配置 → 发信邮箱填 SMTP。")
    to = str(to or "").strip()
    if not to:
        raise ValueError("缺少收件人")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f'{cfg["from_name"]} <{cfg["from_email"]}>'
    msg["To"] = to
    msg.set_content(body)
    host = cfg["host"]
    port = int(cfg.get("port") or 587)
    try:
        if port == 465:
            client = smtplib.SMTP_SSL(host, port, timeout=20)
        else:
            client = smtplib.SMTP(host, port, timeout=20)
            if cfg.get("use_tls") is not False:
                client.starttls()
        client.login(cfg["username"], cfg["password"])
        client.send_message(msg)
        client.quit()
    except Exception as exc:
        SLog.w(TAG, f"send_mail failed to={to}: {type(exc).__name__}")
        raise RuntimeError(f"邮件没发出去：{exc}") from exc


def test_mail(to: str = "") -> dict[str, Any]:
    cfg = get_mail_credentials()
    if not cfg.get("configured"):
        raise RuntimeError("还没有配置发信邮箱")
    dest = str(to or "").strip() or cfg["from_email"]
    send_mail(
        to=dest,
        subject="MiniOrange 发信测试",
        body="这是一封测试信。能收到就说明注册验证码可以发出去。",
    )
    return {"to": dest}
