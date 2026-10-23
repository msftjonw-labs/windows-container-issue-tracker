import os
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path


def main():
    required = (
        "SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD", "SMTP_FROM",
        "WEEKLY_EMAIL_RECIPIENTS", "RUN_STATUS", "RUN_URL", "GITHUB_REPOSITORY",
        "PROJECT_URL", "SNAPSHOT_DIRECTORY", "REPORT_DIRECTORY", "REPORT_STATUS",
    )
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise ValueError("Missing notification configuration: " + ", ".join(missing))

    message = EmailMessage()
    message["From"] = os.environ["SMTP_FROM"]
    message["To"] = os.environ["WEEKLY_EMAIL_RECIPIENTS"]
    message["Subject"] = (
        f'Windows container workflow: scoring {os.environ["RUN_STATUS"]}, '
        f'report {os.environ["REPORT_STATUS"]}: {os.environ["GITHUB_REPOSITORY"]}'
    )
    body = (
        f'Repository: {os.environ["GITHUB_REPOSITORY"]}\n'
        f'Issue scoring run status: {os.environ["RUN_STATUS"]}\n'
        f'Hypothesis report status: {os.environ["REPORT_STATUS"]}\n'
        f'View workflow run: {os.environ["RUN_URL"]}\n'
        f'View ProjectV2 board: {os.environ["PROJECT_URL"]}\n'
    )

    attachments = []
    for directory, pattern, description in (
        ("SNAPSHOT_DIRECTORY", "windows-container-issue-snapshot-*.pdf", "issue snapshot"),
        ("REPORT_DIRECTORY", "windows-container-hypothesis-validation-*.pdf", "hypothesis report"),
    ):
        files = sorted(
            Path(os.environ[directory]).glob(pattern),
            key=lambda path: (path.stat().st_mtime_ns, path.name),
        )
        if not files:
            body += f'Attachment unavailable: {description}; see workflow logs.\n'
            continue
        attachments.append(files[-1])
    message.set_content(body)
    for pdf in attachments:
        message.add_attachment(
            pdf.read_bytes(), maintype="application", subtype="pdf", filename=pdf.name,
        )

    with smtplib.SMTP_SSL(
        os.environ["SMTP_HOST"], 465, context=ssl.create_default_context(), timeout=30
    ) as smtp:
        smtp.login(os.environ["SMTP_USERNAME"], os.environ["SMTP_PASSWORD"])
        smtp.send_message(message)


if __name__ == "__main__":
    main()
