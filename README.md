# windows-container-issue-tracker

This repository scores qualifying Windows-related issues from configured
public repositories and refreshes organization Project v2 #1. The scheduled
GitHub Actions workflow also publishes a weekly digest issue in this repository.
Runs can be started manually from the Actions tab; manual runs update the
Project v2 board but do not publish a digest issue.

## Hypothesis-led ecosystem strategy

Over the next 3–5 years, the provisional goal is to help teams adopt and sustain
production Windows container workloads while making upstream Windows support
easier for independent maintainers to own. Target workloads and segments must be
validated, not inferred from issue popularity.

The automated **Priority Score is a community activity signal, not a strategic
ranking or a funding decision**. It combines sampled comments, distinct GitHub
authors, labels, and repository boosts. Authors are not independent organizations,
and the activity and label filters can exclude quiet but important blockers.
Keep the existing board and weekly digest as discovery inputs; use the issue
forms in **Issues → New issue** to record strategic decisions separately. These
forms do not change scoring, collection thresholds, Project fields, or digest
selection, and strategy records are not automatically added to the Project.

### Outcomes and competing hypotheses

Start with these **unvalidated hypotheses**, not product commitments:

| Hypothesis | Candidate segment/workload | Expected behavior change | Potential disconfirming evidence |
| --- | --- | --- | --- |
| Adoption: deployment and compatibility friction blocks adoption. | Teams evaluating existing Windows application workloads for containers. | Removing a named blocker enables a production deployment. | Teams still decline adoption after the blocker is removed because cost, application fit, or other constraints dominate. |
| Retention: reliability and troubleshooting gaps limit production use. | Operators already running Windows containers in production. | A targeted fix reduces deployment failures or troubleshooting time and supports continued use. | Comparable failures, investigation time, or abandonment persist after the fix is deployed. |
| Ecosystem: upstream maintenance gaps make Windows support too costly. | Maintainers of runtimes, orchestration, and related tooling. | Maintainers sustain Windows testing and support across releases with less intervention. | Support still depends on the same vendor engineers, or maintenance effort does not decrease. |

Create a **Strategy hypothesis** issue for each hypothesis being investigated.
Name an accountable owner, a specific segment and workload, alternatives,
baseline, success and failure criteria, and a review date **before** selecting
experiments. Record unknowns explicitly; the examples above are not evidence.
Do not choose only issues likely to confirm the preferred explanation.

### Three protected workstreams

| Workstream | Admission rule | Hypothesis requirement |
| --- | --- | --- |
| Foundational health | Security, reliability, regressions, or existing commitments. | None; urgent remediation must not wait for an experiment. |
| Strategic experiments | A contribution can test a specific, falsifiable hypothesis. | Link an open hypothesis and explain what result would challenge it. |
| Community discovery | Unexpected needs, quiet blockers, or evidence outside the current thesis. | None; findings may create or overturn a hypothesis. |

At each monthly portfolio review, explicitly reserve capacity for **all three**
workstreams and record the allocation and rationale in a tracking issue.
Do not let one popularity-ranked queue consume discovery or foundational work.
Use established private security reporting channels for vulnerabilities; do not
put exploit details, confidential customer data, or credentials in public issues.

### Contribution decisions

Create a **Contribution assessment and experiment** issue and link the original
upstream issue rather than duplicating its discussion. Assess each dimension
separately; do not collapse unknowns into a misleading numerical score.

| Dimension | Evidence to record |
| --- | --- |
| Strategic relevance | Named target segment and workload, or why this challenges the current strategy. |
| Impact | Production blocker, degraded operation, or inconvenience; include a concrete consequence. |
| Reach | Verified independent organizations affected, evidence source, and uncertainty; never infer organization counts from votes or authors. |
| Learning value | Competing explanations the contribution distinguishes and results that would disprove the hypothesis. |
| Ecosystem leverage | Projects helped, maintainer ownership, testing, and expected ongoing support burden. |
| Feasibility | Maintainer agreement, upstream acceptance path, dependencies, and maintenance capacity. |

For each dimension, record **supported**, **contradicted**, or **unknown**, with
evidence and a rationale. A contradiction is useful learning, not automatically
a rejection. Record a decision to contribute, investigate, defer, or decline,
the trade-offs, and an owner. Never require strategic relevance to approve
foundational work or community discovery.

Use **Community evidence** issues to collect interviews with adopters,
non-adopters, teams that abandoned adoption, and maintainers. Capture negative
and contradictory findings as well as demand. Record consent-safe, anonymized
summaries and public references only; retain sensitive research elsewhere.
Low-activity issues and interview-only needs can enter these workstreams even
when absent from the automated board.

### Experiments and outcome reviews

Before contribution work starts, record the baseline, intended behavior change,
measurement source, target, observation window, and review date. Missing
baselines require an explicit collection step, not an invented value. Ask
maintainers about acceptance and long-term ownership before proposing changes.

Measure outcomes rather than merged PR counts:

- Previously blocked workloads reaching production, with a defined cohort and
  count of attempted versus successful deployments.
- Deployment failure rate per attempt and troubleshooting time for comparable
  workloads before and after the intervention.
- Windows support and tests maintained over named upstream releases.
- Independent contributors and maintainers sustaining support, including review
  and maintenance burden rather than commit counts alone.

At the experiment review, record observed results, confounders, contradictory
evidence, and whether the result supports, contradicts, or remains inconclusive
for the hypothesis. A merged PR alone is not a completed experiment. Record
follow-up and an explicit decision to continue, revise, or stop; revise the
linked hypothesis when evidence disagrees.

Review evidence and workstream balance monthly and revisit the strategic thesis
quarterly against the 3–5-year goal. Each review should identify what changed,
what remains unknown, who owns the next learning step, and when it will be
reviewed. These are human decisions; the tracker does not automatically infer
customer outcomes or validate hypotheses.

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

After a successful refresh, the workflow writes the full ProjectV2 board to
`snapshot-history/windows-container-issue-snapshot-YYYY-MM-DD.pdf` and uploads
the PDF as a run artifact. Scheduled emails and requested manual-run emails
include the ProjectV2 link and attach that run's snapshot.

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
after that job succeeds. The email also links to the ProjectV2 board and
attaches the generated PDF snapshot. Scheduled runs send email automatically. To request
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
