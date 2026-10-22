import json
import os
import smtplib
import ssl
import sys
from datetime import datetime, timezone
from email.message import EmailMessage


def send_weekly_email(html_body):
    smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com").strip()
    smtp_port = int(os.getenv("SMTP_PORT", "465"))
    smtp_security = os.getenv("SMTP_SECURITY", "ssl").strip().lower()
    smtp_username = (
        os.getenv("SMTP_USERNAME")
        or os.getenv("GMAIL_ADDRESS")
        or ""
    ).strip()
    smtp_password = (
        os.getenv("SMTP_PASSWORD")
        or os.getenv("GMAIL_APP_PASSWORD")
        or ""
    )
    from_address = (
        os.getenv("SMTP_FROM_ADDRESS")
        or smtp_username
    ).strip()
    recipients = [
        recipient.strip()
        for recipient in os.getenv(
            "WEEKLY_EMAIL_RECIPIENTS",
            ""
        ).split(",")
        if recipient.strip()
    ]

    if not smtp_host:
        raise ValueError(
            "SMTP_HOST is required when weekly email is enabled."
        )
    if smtp_security not in {"ssl", "starttls"}:
        raise ValueError(
            "SMTP_SECURITY must be either 'ssl' or 'starttls'."
        )
    if not smtp_username:
        raise ValueError(
            "SMTP_USERNAME is required when weekly email is enabled."
        )
    if not smtp_password:
        raise ValueError(
            "SMTP_PASSWORD is required when weekly email is enabled."
        )
    if not from_address:
        raise ValueError(
            "SMTP_FROM_ADDRESS is required when weekly email is enabled."
        )
    if not recipients:
        raise ValueError(
            "WEEKLY_EMAIL_RECIPIENTS must contain at least one address."
        )

    message = EmailMessage()
    message["Subject"] = (
        f"Windows Container Weekly Issue Digest "
        f"- {datetime.now(timezone.utc):%Y-%m-%d}"
    )
    message["From"] = from_address
    message["To"] = ", ".join(recipients)
    message.set_content(
        "This report requires an HTML-capable email client. "
        "Use the durable GitHub digest issue or workflow run for details."
    )
    message.add_alternative(html_body, subtype="html")

    context = ssl.create_default_context()
    if smtp_security == "ssl":
        with smtplib.SMTP_SSL(
            smtp_host,
            smtp_port,
            context=context,
            timeout=30
        ) as smtp:
            smtp.login(smtp_username, smtp_password)
            smtp.send_message(message)
    else:
        with smtplib.SMTP(
            smtp_host,
            smtp_port,
            timeout=30
        ) as smtp:
            smtp.ehlo()
            smtp.starttls(context=context)
            smtp.ehlo()
            smtp.login(smtp_username, smtp_password)
            smtp.send_message(message)

    print(f"Weekly issue digest submitted through {smtp_host}.")


def main():
    payload_path = (
        sys.argv[1]
        if len(sys.argv) > 1
        else "weekly-email-payload.json"
    )
    with open(payload_path, encoding="utf-8") as payload_file:
        payload = json.load(payload_file)
    html_body = payload.get("html") if isinstance(payload, dict) else None
    if not isinstance(html_body, str):
        raise ValueError("Email payload must contain an HTML string.")
    send_weekly_email(html_body)


if __name__ == "__main__":
    main()
