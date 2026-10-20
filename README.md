# windows-container-issue-tracker

## Weekly digest email

The scheduled GitHub Actions workflow can send the generated HTML digest
directly through an SMTP server. This does not use the Power Automate GitHub
or Gmail connectors.

Before using this route, confirm that sending repository data through the
chosen mail service is permitted by your organization's security and DLP
policies. Bypassing a blocked Power Automate connector does not itself grant
approval to use the same service another way.

### Gmail SMTP setup

1. Enable 2-Step Verification on the sending Google account.
2. Create a Google App Password for the account. A normal Google password
   will not work. App Passwords might be unavailable for accounts enrolled in
   Advanced Protection or restricted by a Google Workspace administrator.
3. In the GitHub repository, open **Settings > Secrets and variables >
   Actions**. Create `SMTP_PASSWORD` as a repository secret, and create the
   remaining settings as repository variables:

   | Type | Name | Value |
   |---|---|---|
   | Variable | `SMTP_USERNAME` | Full Gmail address used to authenticate |
   | Secret | `SMTP_PASSWORD` | The 16-character Google App Password |
   | Variable | `SMTP_FROM_ADDRESS` | Sender address, normally the same Gmail address |
   | Variable | `WEEKLY_EMAIL_RECIPIENTS` | Comma-separated recipient addresses |

4. Run **Calculate Issue Priority Scores** manually from the Actions tab,
   select the `emailtest` branch, enable **Send the digest email during this
   manual run**, and use a test mailbox for the first run.

The manual email option defaults to off. Scheduled runs send the email
automatically.

The workflow uses `smtp.gmail.com` on port `465` with implicit TLS. The script
also supports other authenticated SMTP services:

| Setting | Description |
|---|---|
| `SMTP_HOST` | SMTP server hostname |
| `SMTP_PORT` | SMTP port |
| `SMTP_SECURITY` | `ssl` for implicit TLS or `starttls` for explicit TLS |
| `SMTP_USERNAME` | SMTP login |
| `SMTP_PASSWORD` | SMTP password or provider-issued API/app password |
| `SMTP_FROM_ADDRESS` | Message sender; defaults to `SMTP_USERNAME` |
| `WEEKLY_EMAIL_RECIPIENTS` | Comma-separated recipients |

SMTP authentication waits for the server's challenge instead of including
credentials in the initial `AUTH` command, for compatibility with servers that
disconnect on inline authentication responses. Both TLS modes use this exchange.

Transient SMTP connection or login disconnects and network timeouts get up to
three attempts, with 1- and 2-second delays and a new TLS connection each time.
Authentication failures are not retried. Delivery is not retried once sending
starts, to avoid duplicate emails if the server accepted the message before
disconnecting. Exhausted retries still fail the workflow; a published digest
issue remains available.

The older `GMAIL_ADDRESS` and `GMAIL_APP_PASSWORD` environment variables
remain supported by the Python script for compatibility, but the workflow now
uses the provider-neutral names.

### Other delivery options

- Use an organization-approved SMTP relay. Configure its host, port, and TLS
  mode with the settings above.
- Use an approved transactional email provider through its HTTPS API. This
  requires a separate implementation and provider credential.
- Use Microsoft Graph `sendMail` with an Entra application or managed
  identity approved for mail access. This avoids Power Automate connectors
  but requires tenant-admin consent and mailbox authorization.
- Keep the durable weekly-digest GitHub issue and let recipients subscribe to
  its notifications if outbound email services are not approved.
