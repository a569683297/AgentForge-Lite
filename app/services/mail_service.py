"""
邮件通道（找回密码用）
======================
只有一件事：把一封纯文本邮件发出去。发不出去时**不抛异常，返回 False**。

为什么不用第三方 SDK（sendgrid / resend）：
  它们都要一个 API key + 一个外网调用，而"找回密码"在本项目里是低频动作。
  标准库 `smtplib` 够用，配置只要 6 个环境变量，换供应商只改 `.env`。

为什么发失败不抛：
  找回密码的接口**必须**永远回同一句话（防邮箱枚举，见 auth_service 文件头）。
  如果 SMTP 挂了就 500、没挂就 200，那"这个邮箱有没有注册"又能从状态码读出来了。
  所以这里把异常收敛成 False，由调用方决定"只记日志、对外照常回成功"。

配置缺失时的行为（本地开发）：
  `smtp_host` 为空 → 不尝试发送，直接把重置链接写进日志（INFO 级），
  接口在 dev 环境还会通过 `dev_link` 字段把它返回给前端 —— 否则本地没邮箱
  就走不通"找回密码"这条链路，等于这块功能没法验收。
"""

from __future__ import annotations

import smtplib
from email.message import EmailMessage
from email.utils import formataddr

from app.config import settings
from app.core.logging import logger


def is_configured() -> bool:
    """SMTP 是否配置齐全（决定"真发邮件"还是"写日志"）。"""
    return bool(settings.smtp_host and settings.smtp_from)


def send_mail(*, to: str, subject: str, body: str) -> bool:
    """
    发一封纯文本邮件。

    Returns:
        True = 已交给 SMTP 服务器；False = 未配置 / 发送失败（细节在日志里）
    """
    if not is_configured():
        # 未配置时**不静默丢弃**：把内容写进日志，本地能照着链接走完流程
        logger.info("SMTP 未配置，邮件内容改为写日志 to=%s subject=%s\n%s", to, subject, body)
        return False

    message = EmailMessage()
    message["From"] = formataddr(("AgentForge", settings.smtp_from))
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)

    try:
        # SMTP_SSL(465) 与 STARTTLS(587) 是两条不同的路，本项目按端口选：
        # 465 直接用 SSL 握手，587 先明文再 STARTTLS。混用会卡在握手阶段超时。
        if settings.smtp_port == 465:
            with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=10) as smtp:
                if settings.smtp_user:
                    smtp.login(settings.smtp_user, settings.smtp_password)
                smtp.send_message(message)
        else:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as smtp:
                if settings.smtp_starttls:
                    smtp.starttls()
                if settings.smtp_user:
                    smtp.login(settings.smtp_user, settings.smtp_password)
                smtp.send_message(message)
    except Exception as exc:  # noqa: BLE001 —— 见文件头"发失败不抛"
        logger.error("邮件发送失败 to=%s err=%s", to, exc)
        return False

    logger.info("邮件已发送 to=%s subject=%s", to, subject)
    return True


def build_reset_body(link: str, *, ttl_min: int) -> str:
    """重置密码邮件的正文。纯文本，不带 HTML —— 少一个被过滤的理由。"""
    return (
        "你好：\n\n"
        "我们收到了重置 AgentForge 账号密码的请求。点击下面的链接设置新密码：\n\n"
        f"    {link}\n\n"
        f"链接 {ttl_min} 分钟内有效，且只能使用一次。\n"
        "如果这不是你本人的操作，忽略本邮件即可，你的密码不会改变。\n\n"
        "—— AgentForge\n"
    )
