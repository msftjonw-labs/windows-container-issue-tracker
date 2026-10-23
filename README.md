# windows-container-issue-tracker

This repository scores qualifying Windows-related issues from configured
public repositories and refreshes organization Project v2 #1. The scheduled
GitHub Actions workflow also publishes a weekly digest issue in this repository.
Runs can be started manually from the Actions tab; manual runs update the
Project v2 board but do not publish a digest issue.

## Rate-aware Project refresh

The refresh collects and scores every configured repository before changing
the Project. It skips pull requests and issues whose total comment count is
below the activity threshold, then fetches details in batches of up to ten
issues instead of one request per issue. Label rules, lookback windows, scoring
weights, the last-100-comments sample, top ten Project issues per repository,
and top three digest entries per repository are unchanged.

The Project is no longer cleared at the start of a run. A paginated snapshot
allows existing items to be reused and unchanged fields to be skipped. Changed
score and Source fields share one mutation request. Missing issues are added
before any stale items are removed. Collection or update failures stop the run
and prevent stale-item cleanup and digest publication; existing board data is
retained, although updates already applied are not rolled back. Cleanup itself
can also stop partway through, leaving extra stale items rather than removing
the newly selected issues.

An issue is eligible for stale-item cleanup only when it belongs to a monitored
repository and its **Source** field matches that repository's full name. This
field acts as the management marker. Draft items, unrelated repositories, and
items without that marker are preserved. Issues selected by the refresh receive
the Source marker, including previously unmarked items already on the board.

REST and GraphQL share a serial, rate-aware client with separate primary-limit
budgets, 30-second request timeouts, and at least one second between mutation
requests. Low remaining budgets pause until reset; rate-limit responses honor
`Retry-After` or use increasing waits. Requests have at most three attempts,
and rate-limit/transient waits have a cumulative 15-minute budget per run.
Permission errors are not retried, and ambiguous REST writes or partial
GraphQL mutation failures are not blindly replayed. Overlapping scheduled and
manual workflows queue rather than cancel the active refresh.

GitHub limits can still be reached, particularly when another process shares
the same App quota. If recovery exceeds the wait budget, the run fails visibly
instead of treating a failed fetch as an empty repository. Retry after the
quota resets; the next run reconciles any partially applied updates.

Run the mocked regression suite without live credentials:
`GITHUB_APP_TOKEN=test-only-placeholder python3 -m unittest discover -s tests -v`.

## Weekly run email notifications

The workflow runs every Monday at 17:00 UTC. A separate email job depends on
the issue-scoring job and sends its status and a link to the workflow run only
after that job succeeds. Scheduled runs send email automatically. To request
email for a manual run, open **Actions → Calculate Issue Priority Scores →
Run workflow** and enable **Send an email after issue scoring succeeds**
(`send_email`). This option defaults to off and does not publish a weekly
digest. Failed, cancelled, or skipped scoring jobs never send email.
Configure the following under
**Settings → Secrets and variables → Actions**.

In the **Secrets** tab, create:

| Secret | Value |
| --- | --- |
| `SMTP_PASSWORD` | Your SMTP credential (for Gmail, an app password, not your normal password). |

In the **Variables** tab, create:

| Variable | Value |
| --- | --- |
| `SMTP_HOST` | Your SMTP provider's hostname (for Gmail, `smtp.gmail.com`). |
| `SMTP_USERNAME` | Your SMTP account username. |
| `SMTP_FROM` | A sender address authorized by your SMTP provider. |
| `WEEKLY_EMAIL_RECIPIENTS` | One destination email address or a comma-separated list of addresses. |

If you configured notifications previously, copy the host, username, sender,
and recipient settings from secrets into the corresponding variables; those
secrets are no longer used. Only `SMTP_PASSWORD` remains a secret for email delivery.

The sender uses authenticated SMTP over TLS on port 465 with certificate
verification. Gmail app passwords require two-step verification and must be
permitted by your account policy. Never commit credentials or recipient
addresses to the repository.

Missing configuration or delivery failures fail the separate email job without
changing the successful scoring job's result. Check the Actions logs if an
expected email does not arrive. Email cannot be guaranteed if the runner stops,
the job times out, or the email job cannot check out the script.
