# windows-container-issue-tracker

This repository scores qualifying Windows-related issues from configured
public repositories and refreshes organization Project v2 #1. The scheduled
GitHub Actions workflow also publishes a weekly digest issue in this repository.
Runs can be started manually from the Actions tab; manual runs update the
Project v2 board but do not publish a digest issue.

## Weekly run email notifications

The workflow runs every Monday at 17:00 UTC. Its final step emails the scoring
job's status and a link to the workflow run, even when earlier steps fail.
Manual runs do not send email. Delivery requires these repository Actions
secrets under **Settings → Secrets and variables → Actions**:

| Secret | Value |
| --- | --- |
| `SMTP_HOST` | Your SMTP provider's hostname (for Gmail, `smtp.gmail.com`). |
| `SMTP_USERNAME` | Your SMTP account username. |
| `SMTP_PASSWORD` | Your SMTP credential (for Gmail, an app password, not your normal password). |
| `SMTP_FROM` | A sender address authorized by your SMTP provider. |
| `NOTIFICATION_EMAIL_TO` | Your destination email address. |

The sender uses authenticated SMTP over TLS on port 465 with certificate
verification. Gmail app passwords require two-step verification and must be
permitted by your account policy. Never commit credentials or recipient
addresses to the repository.

Missing configuration or delivery failures fail the notification step; the
email reports the scoring job's status before that step. Check the Actions
logs if an expected email does not arrive. Email cannot be guaranteed if the
runner stops, the job times out, or checkout fails before the script is available.
