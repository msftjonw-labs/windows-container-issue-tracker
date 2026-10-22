# windows-container-issue-tracker

## Weekly digest email

The GitHub Actions workflow first refreshes the GitHub Project v2 board and
publishes the scheduled digest issue. When email is enabled, a separate job
sends the generated HTML digest through SMTP only after the scoring and project
sync job succeeds. This does not use the Power Automate GitHub or Gmail
connectors.

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
   settings below as repository variables. `SMTP_HOST` is optional and defaults
   to `smtp.gmail.com`; `SMTP_SECURITY` defaults to `starttls` and `SMTP_PORT`
   defaults to `587` for STARTTLS or `465` for SSL.

   | Type | Name | Value |
   |---|---|---|
   | Variable | `SMTP_USERNAME` | Full Gmail address used to authenticate |
   | Secret | `SMTP_PASSWORD` | The 16-character Google App Password |
   | Variable | `SMTP_FROM` | Sender address, normally the same Gmail address |
   | Variable | `SMTP_TO` | Comma-separated recipient addresses |

4. Run **Calculate Issue Priority Scores** manually from the Actions tab,
   select the `emailtest` branch, enable **Send the digest email during this
   manual run**, and use a test mailbox for the first run.

The manual email option defaults to off. Scheduled runs send the email
automatically. SMTP configuration is used only by the separate email job; an
email failure is reported independently and does not undo a successful project
sync.

The workflow defaults to `smtp.gmail.com` with STARTTLS on port `587`. The
script also supports implicit TLS and authenticated or unauthenticated SMTP
relays:

| Setting | Description |
|---|---|
| `SMTP_HOST` | SMTP server hostname |
| `SMTP_PORT` | SMTP port |
| `SMTP_SECURITY` | `ssl` for implicit TLS or `starttls` for explicit TLS |
| `SMTP_USERNAME` | SMTP login |
| `SMTP_PASSWORD` | SMTP password or provider-issued API/app password |
| `SMTP_FROM` | Message sender |
| `SMTP_TO` | Comma-separated recipients |

`SMTP_FROM_ADDRESS` and `WEEKLY_EMAIL_RECIPIENTS` remain supported as fallback
names for sender and recipient settings. `SMTP_USERNAME` and `SMTP_PASSWORD`
must either both be provided or both be omitted for an unauthenticated relay.

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
