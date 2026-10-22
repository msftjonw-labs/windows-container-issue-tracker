import os
import smtplib
import ssl
from email.message import EmailMessage


def main():
    required = (
        "SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD", "SMTP_FROM",
        "NOTIFICATION_EMAIL_TO", "RUN_STATUS", "RUN_URL", "GITHUB_REPOSITORY",
    )
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise ValueError("Missing notification configuration: " + ", ".join(missing))

    message = EmailMessage()
    message["From"] = os.environ["SMTP_FROM"]
    message["To"] = os.environ["NOTIFICATION_EMAIL_TO"]
    message["Subject"] = (
        f'Weekly run {os.environ["RUN_STATUS"]}: {os.environ["GITHUB_REPOSITORY"]}'
    )
    message.set_content(
        f'Repository: {os.environ["GITHUB_REPOSITORY"]}\n'
        f'Scheduled scoring run status: {os.environ["RUN_STATUS"]}\n'
        f'View workflow run: {os.environ["RUN_URL"]}\n'
    )

    with smtplib.SMTP_SSL(
        os.environ["SMTP_HOST"], 465, context=ssl.create_default_context(), timeout=30
    ) as smtp:
        smtp.login(os.environ["SMTP_USERNAME"], os.environ["SMTP_PASSWORD"])
        smtp.send_message(message)


if __name__ == "__main__":
    main()
