import json
import os
import smtplib
import ssl
import sys
from email.message import EmailMessage


def send_weekly_email(html_body):
    smtp_host = os.getenv("SMTP_HOST", "").strip()
    if not smtp_host:
        print("SMTP_HOST is not configured; skipping weekly email.")
        return
    if "@" in smtp_host:
        raise ValueError(
            "SMTP_HOST must be an SMTP server hostname, not an email address."
        )

    from_address = (
        os.getenv("SMTP_FROM", "")
        or os.getenv("SMTP_FROM_ADDRESS", "")
    ).strip()
    recipients = [
        recipient.strip()
        for recipient in (
            os.getenv("SMTP_TO")
            or os.getenv("WEEKLY_EMAIL_RECIPIENTS", "")
        ).split(",")
        if recipient.strip()
    ]
    if not from_address or not recipients:
        raise ValueError(
            "SMTP_FROM and SMTP_TO are required when SMTP_HOST is set."
        )

    smtp_security = os.getenv("SMTP_SECURITY", "starttls").strip().lower()
    if smtp_security not in {"starttls", "ssl"}:
        raise ValueError("SMTP_SECURITY must be starttls or ssl.")
    default_port = 465 if smtp_security == "ssl" else 587
    smtp_port = int(os.getenv("SMTP_PORT") or default_port)
    if not 1 <= smtp_port <= 65535:
        raise ValueError("SMTP_PORT must be between 1 and 65535.")

    smtp_username = os.getenv("SMTP_USERNAME", "")
    smtp_password = os.getenv("SMTP_PASSWORD", "")
    if bool(smtp_username) != bool(smtp_password):
        raise ValueError("SMTP_USERNAME and SMTP_PASSWORD must be set together.")

    message = EmailMessage()
    repository = os.getenv(
        "GITHUB_REPOSITORY",
        "msftjonw-labs/windows-container-issue-tracker"
    )
    workflow = os.getenv("GITHUB_WORKFLOW", "Windows Container Issue Tracker")
    server_url = os.getenv("GITHUB_SERVER_URL", "https://github.com").rstrip("/")
    run_id = os.getenv("GITHUB_RUN_ID", "")
    run_url = f"{server_url}/{repository}/actions/runs/{run_id}"
    message["Subject"] = f"{workflow}: sync succeeded"
    message["From"] = from_address
    message["To"] = ", ".join(recipients)
    message.set_content(
        f"The scoring and project sync completed successfully for {repository}.\n\n"
        f"Workflow run: {run_url}\n\n"
        "The weekly issue digest is included as an HTML message."
    )
    message.add_alternative(html_body, subtype="html")

    context = ssl.create_default_context()
    if smtp_security == "ssl":
        connection = smtplib.SMTP_SSL(
            smtp_host, smtp_port, timeout=30, context=context
        )
    else:
        connection = smtplib.SMTP(smtp_host, smtp_port, timeout=30)
    with connection as smtp:
        if smtp_security == "starttls":
            smtp.ehlo()
            smtp.starttls(context=context)
            smtp.ehlo()
        if smtp_username:
            smtp.login(smtp_username, smtp_password)
        refused = smtp.send_message(
            message,
            from_addr=from_address,
            to_addrs=recipients
        )
        if refused:
            raise RuntimeError("SMTP server refused one or more recipients.")

    print("Weekly issue digest email sent.")


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
