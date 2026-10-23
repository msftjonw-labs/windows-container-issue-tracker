import os
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path


def main():
    required = (
        "SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD", "SMTP_FROM",
        "WEEKLY_EMAIL_RECIPIENTS", "RUN_STATUS", "RUN_URL", "GITHUB_REPOSITORY",
        "PROJECT_URL", "SNAPSHOT_DIRECTORY",
    )
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise ValueError("Missing notification configuration: " + ", ".join(missing))

    message = EmailMessage()
    message["From"] = os.environ["SMTP_FROM"]
    message["To"] = os.environ["WEEKLY_EMAIL_RECIPIENTS"]
    message["Subject"] = (
        f'Issue scoring run {os.environ["RUN_STATUS"]}: {os.environ["GITHUB_REPOSITORY"]}'
    )
    message.set_content(
        f'Repository: {os.environ["GITHUB_REPOSITORY"]}\n'
        f'Issue scoring run status: {os.environ["RUN_STATUS"]}\n'
        f'View workflow run: {os.environ["RUN_URL"]}\n'
        f'View ProjectV2 board: {os.environ["PROJECT_URL"]}\n'
    )

    snapshots = sorted(
        Path(os.environ["SNAPSHOT_DIRECTORY"]).glob("windows-container-issue-snapshot-*.pdf"),
        key=lambda path: (path.stat().st_mtime_ns, path.name),
    )
    if not snapshots:
        raise FileNotFoundError(
            f"No issue snapshot PDF found in {os.environ['SNAPSHOT_DIRECTORY']}"
        )
    snapshot = snapshots[-1]
    message.add_attachment(
        snapshot.read_bytes(),
        maintype="application",
        subtype="pdf",
        filename=snapshot.name,
    )

    with smtplib.SMTP_SSL(
        os.environ["SMTP_HOST"], 465, context=ssl.create_default_context(), timeout=30
    ) as smtp:
        smtp.login(os.environ["SMTP_USERNAME"], os.environ["SMTP_PASSWORD"])
        smtp.send_message(message)


if __name__ == "__main__":
    main()
