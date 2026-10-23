# windows-container-issue-tracker

This repository scores qualifying Windows-related issues from configured
public repositories and refreshes organization Project v2 #1. The scheduled
GitHub Actions workflow also publishes a weekly digest issue in this repository.
Runs can be started manually from the Actions tab; manual runs update the
Project v2 board but do not publish a digest issue.

## Weekly run email notifications

The workflow runs every Monday at 17:00 UTC. A separate email job depends on
the issue-scoring job and sends its status and a link to the workflow run only
after that job succeeds. Failed, cancelled, or skipped scoring jobs and manual
runs do not send email. Delivery requires these repository Actions
secrets under **Settings → Secrets and variables → Actions**:

| Secret | Value |
| --- | --- |
| `SMTP_HOST` | Your SMTP provider's hostname (for Gmail, `smtp.gmail.com`). |
| `SMTP_USERNAME` | Your SMTP account username. |
| `SMTP_PASSWORD` | Your SMTP credential (for Gmail, an app password, not your normal password). |
| `SMTP_FROM` | A sender address authorized by your SMTP provider. |
| `WEEKLY_EMAIL_RECIPIENTS` | One destination email address or a comma-separated list of addresses. |

If you configured notifications previously, copy the former recipient secret's
value into `WEEKLY_EMAIL_RECIPIENTS`; the old secret is no longer used.

The sender uses authenticated SMTP over TLS on port 465 with certificate
verification. Gmail app passwords require two-step verification and must be
permitted by your account policy. Never commit credentials or recipient
addresses to the repository.

Missing configuration or delivery failures fail the separate email job without
changing the successful scoring job's result. Check the Actions logs if an
expected email does not arrive. Email cannot be guaranteed if the runner stops,
the job times out, or the email job cannot check out the script.
