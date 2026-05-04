"""Gmail SMTP sender — stdlib smtplib, no third-party deps.

Reads GMAIL_USERNAME and GMAIL_APP_PASSWORD from the environment.
The cron wrapper (`scripts/run_monitor.sh`) sources `.env` before
invoking python, so these vars are already loaded by the time
notify_subscribers runs.
"""
from __future__ import annotations

import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

GMAIL_SMTP_HOST = "smtp.gmail.com"
GMAIL_SMTP_PORT = 465  # SSL


class SMTPNotConfigured(RuntimeError):
    """Raised when GMAIL_USERNAME / GMAIL_APP_PASSWORD aren't set."""


def send_email(
    *,
    to_email: str,
    subject: str,
    html_body: str,
    text_body: str,
    from_email: str | None = None,
    timeout: int = 30,
) -> None:
    """Send a single multipart/alternative email via Gmail SMTP.

    Raises SMTPNotConfigured if env vars are missing. Other SMTP errors
    propagate as smtplib exceptions — the orchestrator catches and logs.
    """
    username = os.environ.get("GMAIL_USERNAME")
    app_password = os.environ.get("GMAIL_APP_PASSWORD")
    if not username or not app_password:
        raise SMTPNotConfigured(
            "GMAIL_USERNAME and GMAIL_APP_PASSWORD must be set in the "
            "environment. Generate an App Password at "
            "https://myaccount.google.com/apppasswords and add both to "
            "your .env file."
        )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_email or username
    msg["To"] = to_email
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP_SSL(GMAIL_SMTP_HOST, GMAIL_SMTP_PORT, timeout=timeout) as smtp:
        smtp.login(username, app_password)
        smtp.send_message(msg)
