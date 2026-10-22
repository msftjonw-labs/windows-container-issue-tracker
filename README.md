# windows-container-issue-tracker

## Weekly digest

The scheduled GitHub Actions workflow refreshes issue priority scores and
publishes a durable weekly digest issue in this repository. The digest is
labeled `weekly-digest`; subscribe to that label or the issue to receive GitHub
notifications.

## Success email notifications

After scoring and any scheduled digest publication succeed, scheduled and manual
runs can send an email containing the issue summary and workflow run link.
Set these Actions settings (passed to the script as environment variables):

- Secret `EMAIL_TO`: your personal Gmail address (or another recipient).
- Variable `SMTP_USERNAME`: the Gmail account used to send the notification.
- Secret `SMTP_PASSWORD`: that account's Google app password, not its regular password.
  Enable two-step verification on the sending account and create an app password.

SMTP defaults to `smtp.gmail.com` on port `465`, using certificate-verified
implicit TLS. Override these with repository Actions variables `SMTP_SERVER`
and `SMTP_PORT` if using another implicit-TLS SMTP provider.

Without `EMAIL_TO`, notification delivery is skipped so existing runs still work.
Once a recipient is configured, missing sender credentials or delivery failures
fail the workflow rather than silently losing the notification. No success email
is sent if scoring or digest publication fails.
