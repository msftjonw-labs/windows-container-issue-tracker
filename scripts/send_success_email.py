import os
import smtplib
import ssl
from email.message import EmailMessage


def send_success_email():
    host = os.getenv("SMTP_HOST", "").strip()
    if not host:
        print("SMTP_HOST is not configured; skipping success email.")
        return
    if "@" in host:
        raise ValueError(
            "SMTP_HOST must be an SMTP server hostname, not an email address. "
            "Set SMTP_HOST to your provider's SMTP server "
            "(for Gmail, smtp.gmail.com). Use SMTP_FROM and SMTP_USERNAME for email addresses."
        )

    sender = os.getenv("SMTP_FROM", "").strip()
    recipients = [
        address.strip()
        for address in os.getenv("SMTP_TO", "").split(",")
        if address.strip()
    ]
    if not sender or not recipients:
        raise ValueError("SMTP_FROM and SMTP_TO are required when SMTP_HOST is set.")

    security = os.getenv("SMTP_SECURITY", "starttls").strip().lower()
    if security not in ("starttls", "ssl"):
        raise ValueError("SMTP_SECURITY must be starttls or ssl.")
    port = int(os.getenv("SMTP_PORT") or (465 if security == "ssl" else 587))
    if not 1 <= port <= 65535:
        raise ValueError("SMTP_PORT must be between 1 and 65535.")
    username = os.getenv("SMTP_USERNAME", "")
    password = os.getenv("SMTP_PASSWORD", "")
    if bool(username) != bool(password):
        raise ValueError("SMTP_USERNAME and SMTP_PASSWORD must be set together.")

    repository = os.getenv("GITHUB_REPOSITORY", "windows-container-issue-tracker")
    workflow = os.getenv("GITHUB_WORKFLOW", "Issue priority scoring")
    server_url = os.getenv("GITHUB_SERVER_URL", "https://github.com").rstrip("/")
    run_id = os.getenv("GITHUB_RUN_ID", "")
    run_url = f"{server_url}/{repository}/actions/runs/{run_id}"
    message = EmailMessage()
    message["Subject"] = f"{workflow}: scoring succeeded"
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message.set_content(
        f"The scoring and project sync completed successfully for {repository}.\n\n"
        f"Workflow run: {run_url}\n"
    )

    context = ssl.create_default_context()
    if security == "ssl":
        connection = smtplib.SMTP_SSL(host, port, timeout=30, context=context)
    else:
        connection = smtplib.SMTP(host, port, timeout=30)
    with connection as smtp:
        if security == "starttls":
            smtp.starttls(context=context)
        if username:
            smtp.login(username, password)
        refused = smtp.send_message(message, from_addr=sender, to_addrs=recipients)
        if refused:
            raise RuntimeError("SMTP server refused one or more recipients.")
    print("Success email sent.")


def main():
    send_success_email()


if __name__ == "__main__":
    main()
