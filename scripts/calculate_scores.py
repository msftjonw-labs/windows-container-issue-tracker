import html
import os
import smtplib
import sys
import time
from datetime import datetime, timedelta, timezone
import requests

if __package__:
    from .send_success_email import send_success_email
else:
    from send_success_email import send_success_email

# -------------------------------------------------------------------
# Configuration & Environment Setup
# -------------------------------------------------------------------
GH_TOKEN = os.getenv("GITHUB_APP_TOKEN") or os.getenv("GH_PAT")
ORGANIZATION_NAME = os.getenv("ORGANIZATION_NAME", "msftjonw-labs")
PROJECT_NUMBER = int(os.getenv("PROJECT_NUMBER", "1"))
SCORE_FIELD_NAME = os.getenv("CUSTOM_FIELD_NAME", "Priority Score")
REPO_FIELD_NAME = os.getenv("REPO_FIELD_NAME", "Source")
DIGEST_REPOSITORY = os.getenv(
    "DIGEST_REPOSITORY",
    "msftjonw-labs/windows-container-issue-tracker"
)
DIGEST_LABELS = [
    label.strip()
    for label in os.getenv(
        "DIGEST_LABELS",
        "weekly-digest"
    ).split(",")
    if label.strip()
]

if not GH_TOKEN:
    print("Error: Missing required environment variable GITHUB_APP_TOKEN.", file=sys.stderr)
    sys.exit(1)

TARGET_REPOS_RAW = os.getenv("TARGET_REPOS", "")
TARGET_REPOS = [r.strip() for r in TARGET_REPOS_RAW.split(",") if r.strip()]

# Activity Threshold Configuration
MIN_RECENT_COMMENTS = int(os.getenv("MIN_RECENT_COMMENTS", "2"))
MAX_PROJECT_ISSUES_PER_REPO = 10

# Scoring Weights & Repository Priority Boosts
WEIGHT_UNIQUE_USERS = 3.0
WEIGHT_TOTAL_COMMENTS = 1.0

LABEL_WEIGHTS = {
    "bug": 5.0,
    "customer-reported": 10.0,
    "p1": 15.0,
    "p2": 8.0,
    "feature-request": 2.0
}

# Repository Priority Boosts
REPO_WEIGHTS = {
    "microsoft/windows-containers": 100.0,
    "microsoft/windows-container-tools": 100.0,
    "azure/aks": 75.0,
    "kubernetes/kubernetes": 0.0,
    "kubernetes/enhancements": 0.0,
    "kubernetes/community": 0.0,
    "moby/moby": 0.0,
    "containerd/containerd": 0.0
}

# API Endpoint Configurations
GH_GRAPHQL_URL = "https://api.github.com/graphql"
GH_REST_URL = "https://api.github.com"
headers = {
    "Authorization": f"Bearer {GH_TOKEN}",
    "Accept": "application/vnd.github+json"
}

MAX_ATTEMPTS = 3
TRANSIENT_STATUS_CODES = {500, 502, 503, 504}
TRANSIENT_ERROR_MARKERS = ("something went wrong while executing your query", "timeout")
# Retrieve open issues updated within the past five years while retaining the
# 730-day activity requirement below.
ISSUE_LOOKBACK_DAYS = 1825
ISSUE_LOOKBACK_START = datetime.now(timezone.utc) - timedelta(days=ISSUE_LOOKBACK_DAYS)
ISSUE_LOOKBACK_TIMESTAMP = ISSUE_LOOKBACK_START.isoformat()

STANDARD_LOOKBACK_DAYS = 730
TWO_YEARS_AGO = datetime.now(timezone.utc) - timedelta(days=STANDARD_LOOKBACK_DAYS)
SINCE_TWO_YEARS_TIMESTAMP = TWO_YEARS_AGO.isoformat()

MAX_GRAPHQL_ATTEMPTS = 3
TRANSIENT_STATUS_CODES = {500, 502, 503, 504}
TRANSIENT_ERROR_MARKERS = ("something went wrong while executing your query", "timeout")
ISSUE_PAGE_SIZE = 10

def run_graphql(query, variables=None):
    for attempt in range(1, MAX_ATTEMPTS + 1):
        response = requests.post(
            GH_GRAPHQL_URL,
            json={"query": query, "variables": variables},
            headers=headers
        )
        if response.status_code != 200:
            error = Exception(f"GraphQL query failed ({response.status_code}): {response.text}")
            transient = response.status_code in TRANSIENT_STATUS_CODES
        else:
            res_data = response.json()
            if "errors" not in res_data:
                return res_data["data"]
            error = Exception(f"GraphQL Errors: {res_data['errors']}")
            transient = any(m in str(res_data["errors"]).lower() for m in TRANSIENT_ERROR_MARKERS)
        if not transient or attempt == MAX_ATTEMPTS:
            raise error
        delay = 2 ** (attempt - 1)
        print(f"  └─ Transient GraphQL failure (attempt {attempt}/{MAX_ATTEMPTS}); retrying in {delay}s...")
        time.sleep(delay)

# -------------------------------------------------------------------
# 1. Organization GitHub Project v2 Discovery & Board Cleanup
# -------------------------------------------------------------------
def get_project_and_field_ids():
    query = """
    query($org: String!, $number: Int!) {
      organization(login: $org) {
        projectV2(number: $number) {
          id
          fields(first: 50) {
            nodes {
              ... on ProjectV2Field {
                id
                name
              }
            }
          }
        }
      }
    }
    """
    data = run_graphql(query, {"org": ORGANIZATION_NAME, "number": PROJECT_NUMBER})
    
    org_data = data.get("organization")
    if not org_data:
        raise ValueError(f"Organization '{ORGANIZATION_NAME}' not found or token lacks permissions.")
        
    project = org_data.get("projectV2")
    if not project:
        raise ValueError(f"Project #{PROJECT_NUMBER} not found under organization '{ORGANIZATION_NAME}'")
        
    project_id = project["id"]
    score_field_id = None
    repo_field_id = None

    for field in project["fields"]["nodes"]:
        if not field:
            continue
        field_name = field.get("name")
        if field_name == SCORE_FIELD_NAME:
            score_field_id = field["id"]
        elif field_name == REPO_FIELD_NAME:
            repo_field_id = field["id"]
            
    if not score_field_id:
        raise ValueError(f"Custom field '{SCORE_FIELD_NAME}' not found in Project #{PROJECT_NUMBER}")
    if not repo_field_id:
        raise ValueError(f"Custom field '{REPO_FIELD_NAME}' not found in Project #{PROJECT_NUMBER}. Please create a Text field named '{REPO_FIELD_NAME}' in your Project board.")
        
    return project_id, score_field_id, repo_field_id

def clear_project_board(project_id):
    query = """
    query($projectId: ID!) {
      node(id: $projectId) {
        ... on ProjectV2 {
          items(first: 100) {
            nodes {
              id
            }
          }
        }
      }
    }
    """
    delete_mutation = """
    mutation($projectId: ID!, $itemId: ID!) {
      deleteProjectV2Item(input: {projectId: $projectId, itemId: $itemId}) {
        deletedItemId
      }
    }
    """
    deleted_count = 0

    while True:
        data = run_graphql(query, {"projectId": project_id})
        items = data.get("node", {}).get("items", {}).get("nodes", [])
        if not items:
            break

        print(f"Clearing batch of {len(items)} existing Project items...")
        for item in items:
            run_graphql(
                delete_mutation,
                {"projectId": project_id, "itemId": item["id"]}
            )
            deleted_count += 1

    if deleted_count == 0:
        print("Board is already empty.")
    else:
        print(
            f"Project board pre-clearing complete "
            f"({deleted_count} items removed).\n"
        )

# -------------------------------------------------------------------
# 2. Label Resolution & Fetching (730-Day Activity Window)
# -------------------------------------------------------------------
def get_required_label_for_repo(repo_full_name):
    repo_lower = repo_full_name.lower()
    owner = repo_lower.split("/", 1)[0]

    if owner == "microsoft" or repo_lower == "docker/for-win":
        return None
    elif repo_lower == "azure/aks":
        return "windows"
    elif repo_lower in ["kubernetes/kubernetes", "kubernetes/enhancements", "kubernetes/community"]:
        return "sig/windows"
    elif repo_lower == "moby/buildkit":
        return "area/windows"
    elif repo_lower == "containerd/nerdctl":
        return "platform/windows"
    elif repo_lower in ["moby/moby", "containerd/containerd"]:
        return "platform/windows"
    else:
        return "sig/windows"

def get_issue_lookback_timestamp(repo_full_name):
    owner = repo_full_name.lower().split("/", 1)[0]
    if owner in {"microsoft", "azure"}:
        return ISSUE_LOOKBACK_TIMESTAMP
    return SINCE_TWO_YEARS_TIMESTAMP

def label_matches_repo(repo_full_name, label_names, required_label):
    repo_lower = repo_full_name.lower()

    if repo_lower == "azure/aks":
        return any(label in {"windows", "win"} for label in label_names)
    if repo_lower in {"moby/buildkit", "moby/moby"}:
        target = required_label.strip().lower()
        return any(label.startswith(target) for label in label_names)
    if repo_lower == "containerd/nerdctl":
        return any(label.startswith("platform/windows") for label in label_names)
    if required_label:
        return required_label.strip().lower() in label_names
    return True

def fetch_issue_graphql_details(node_id):
    query = """
    query($id: ID!) {
      node(id: $id) {
        ... on Issue {
          id
          number
          title
          url
          comments(last: 100) {
            nodes {
              createdAt
              author { login }
            }
          }
        }
      }
    }
    """
    data = run_graphql(query, {"id": node_id})
    return data.get("node")

def fetch_external_repo_issues(repo_full_name):
    parts = repo_full_name.strip().split("/")
    if len(parts) != 2:
        print(f"Skipping invalid target format '{repo_full_name}'. Expected 'owner/repo'.")
        return []

    owner, repo = parts[0], parts[1]
    required_label = get_required_label_for_repo(repo_full_name)
    issue_lookback_timestamp = get_issue_lookback_timestamp(repo_full_name)
    
    url = f"{GH_REST_URL}/repos/{owner}/{repo}/issues"
    params = {
        "state": "open",
        "per_page": 100,
        "sort": "updated",
        "direction": "desc",
        "since": issue_lookback_timestamp
    }

    client_side_label_repos = {
        "azure/aks",
        "moby/buildkit",
        "moby/moby",
        "containerd/nerdctl"
    }
    if required_label and repo_full_name.lower() not in client_side_label_repos:
        print(f"  └─ Exact Label Requirement: '{required_label}' | Activity Threshold: >= {MIN_RECENT_COMMENTS} comments in past 730 days")
        params["labels"] = required_label
    else:
        print(f"  └─ Fetching Open Issues | Activity Threshold: >= {MIN_RECENT_COMMENTS} comments in past 730 days")
    print(f"  └─ Issue Lookback Start: {issue_lookback_timestamp[:10]}")

    verified_issues = []
    page = 1

    while True:
        params["page"] = page
        response = requests.get(url, headers=headers, params=params.copy())
        if response.status_code != 200:
            print(f"  └─ Failed to fetch issues from REST API ({response.status_code}): {response.text}")
            return []

        raw_issues = response.json()
        for issue in raw_issues:
            if "pull_request" in issue:
                continue

            label_names = [
                label["name"].strip().lower()
                for label in issue.get("labels", [])
                if isinstance(label, dict) and "name" in label
            ]

            if not label_matches_repo(
                repo_full_name,
                label_names,
                required_label
            ):
                print(
                    f"  └─ [EXCLUDED - LABEL MISMATCH] "
                    f"Issue #{issue['number']} missing Windows label "
                    f"matching '{required_label}'"
                )
                continue

            gql_details = fetch_issue_graphql_details(issue["node_id"])
            if not gql_details:
                continue

            comments = gql_details.get("comments", {}).get("nodes", [])

            recent_comments_count = 0
            for comment in comments:
                if comment and comment.get("createdAt"):
                    comment_dt = datetime.fromisoformat(comment["createdAt"].replace("Z", "+00:00"))
                    if comment_dt >= TWO_YEARS_AGO:
                        recent_comments_count += 1

            if recent_comments_count < MIN_RECENT_COMMENTS:
                print(f"  └─ [EXCLUDED - INACTIVE] Issue #{issue['number']} has {recent_comments_count} comment(s) in past 730 days (Threshold: >= {MIN_RECENT_COMMENTS})")
                continue

            gql_details["raw_label_names"] = label_names
            gql_details["recent_comments_count"] = recent_comments_count
            gql_details["repo_full_name"] = repo_full_name
            verified_issues.append(gql_details)

        if "next" not in response.links:
            break
        page += 1

    return verified_issues

# -------------------------------------------------------------------
# 3. Weighted Priority Score Calculation with Repo Boost
# -------------------------------------------------------------------
def compute_priority_score(issue):
    comments = issue.get("comments", {}).get("nodes", [])
    
    authors = {c["author"]["login"] for c in comments if c and c.get("author")}
    unique_user_count = len(authors)
    total_comments = len(comments)
    
    label_names = issue.get("raw_label_names", [])
    label_score = sum(LABEL_WEIGHTS.get(label, 0.0) for label in label_names)
    
    # Repository Boost
    repo_name = issue.get("repo_full_name", "").lower()
    repo_boost = REPO_WEIGHTS.get(repo_name, 0.0)
    
    final_score = (
        (unique_user_count * WEIGHT_UNIQUE_USERS) +
        (total_comments * WEIGHT_TOTAL_COMMENTS) +
        label_score +
        repo_boost
    )
    
    return round(final_score, 2)

# -------------------------------------------------------------------
# 4. Project v2 Mutation Sync (Score + Source Text Column)
# -------------------------------------------------------------------
def sync_to_github_project(project_id, score_field_id, repo_field_id, issue_node_id, repo_full_name, score):
    # 1. Add item to project
    add_item_mutation = """
    mutation($projectId: ID!, $contentId: ID!) {
      addProjectV2ItemById(input: {projectId: $projectId, contentId: $contentId}) {
        item { 
          id 
          type
        }
      }
    }
    """
    item_data = run_graphql(add_item_mutation, {"projectId": project_id, "contentId": issue_node_id})
    item_node = item_data["addProjectV2ItemById"]["item"]
    item_id = item_node["id"]
    item_type = item_node.get("type", "UNKNOWN")

    # Hydration pause for external reference items (e.g., moby/moby)
    if item_type != "ISSUE" or "moby" in repo_full_name.lower():
        time.sleep(0.5)

    # 2. Update Priority Score (Number Field)
    update_score_mutation = """
    mutation($projectId: ID!, $itemId: ID!, $scoreFieldId: ID!, $scoreValue: Float!) {
      updateProjectV2ItemFieldValue(
        input: {
          projectId: $projectId
          itemId: $itemId
          fieldId: $scoreFieldId
          value: { number: $scoreValue }
        }
      ) {
        projectV2Item { id }
      }
    }
    """
    run_graphql(update_score_mutation, {
        "projectId": project_id,
        "itemId": item_id,
        "scoreFieldId": score_field_id,
        "scoreValue": float(score)
    })

    # 3. Update Source (Text Field with Explicit Variable Names and Retry Loop)
    update_repo_mutation = """
    mutation($projectId: ID!, $itemId: ID!, $repoFieldId: ID!, $textValue: String!) {
      updateProjectV2ItemFieldValue(
        input: {
          projectId: $projectId
          itemId: $itemId
          fieldId: $repoFieldId
          value: { text: $textValue }
        }
      ) {
        projectV2Item { id }
      }
    }
    """
    
    for attempt in range(1, 3):
        try:
            run_graphql(update_repo_mutation, {
                "projectId": project_id,
                "itemId": item_id,
                "repoFieldId": repo_field_id,
                "textValue": str(repo_full_name)
            })
            break
        except Exception as e:
            if attempt == 2:
                print(f"  └─ [WARNING] Could not update Source field for {repo_full_name} Item #{item_id}: {e}")
            time.sleep(1.0)

# -------------------------------------------------------------------
# 5. Durable Weekly Digest
# -------------------------------------------------------------------
def build_digest_title():
    run_id = os.getenv("GITHUB_RUN_ID", "unknown")
    generated_at = datetime.now(timezone.utc)
    return (
        f"[weekly-digest] Windows Container Issue Priorities "
        f"- {generated_at:%Y-%m-%d} - Run {run_id}"
    )

def build_digest_body(top_issues_by_repo):
    generated_at = datetime.now(timezone.utc)
    workflow_run_url = os.getenv("GITHUB_WORKFLOW_RUN_URL", "")
    rows = []

    for repo_name, scored_issues in top_issues_by_repo.items():
        safe_repo_name = html.escape(repo_name)

        if not scored_issues:
            rows.append(
                "<tr>"
                f"<td>{safe_repo_name}</td>"
                '<td colspan="4"><em>No qualifying issues</em></td>'
                "</tr>"
            )
            continue

        for issue, score in scored_issues:
            issue_number = issue["number"]
            issue_title = html.escape(issue["title"])
            issue_url = html.escape(issue["url"], quote=True)
            recent_comments = issue.get("recent_comments_count", 0)
            labels = ", ".join(issue.get("raw_label_names", []))

            rows.append(
                "<tr>"
                f"<td>{safe_repo_name}</td>"
                f'<td><a href="{issue_url}">'
                f"#{issue_number} - {issue_title}</a></td>"
                f'<td style="text-align:right">{score:.2f}</td>'
                f'<td style="text-align:right">{recent_comments}</td>'
                f"<td>{html.escape(labels)}</td>"
                "</tr>"
            )

    workflow_link = ""
    if workflow_run_url:
        safe_workflow_url = html.escape(workflow_run_url, quote=True)
        workflow_link = (
            f'<p><a href="{safe_workflow_url}">'
            "View the generating GitHub Actions run"
            "</a></p>"
        )

    run_id = html.escape(os.getenv("GITHUB_RUN_ID", "unknown"))
    return f"""
<!-- weekly-digest -->
<!-- generated-at: {generated_at.isoformat()} -->
<!-- workflow-run-id: {run_id} -->

<h2>Weekly Windows Container Issue Priorities</h2>

<p>
Top three qualifying issues from each monitored repository,
ordered by priority score.
</p>

<table>
<thead>
<tr>
<th>Repository</th>
<th>Issue</th>
<th>Priority Score</th>
<th>Recent Comments</th>
<th>Labels</th>
</tr>
</thead>
<tbody>
{''.join(rows)}
</tbody>
</table>

<p>Generated at {generated_at:%Y-%m-%d %H:%M UTC}.</p>

{workflow_link}
""".strip()

def find_existing_digest_issue(repo_full_name, run_id):
    owner, repo = repo_full_name.split("/", 1)
    params = {
        "state": "all",
        "per_page": 100
    }
    if DIGEST_LABELS:
        params["labels"] = DIGEST_LABELS[0]

    response = requests.get(
        f"{GH_REST_URL}/repos/{owner}/{repo}/issues",
        headers=headers,
        params=params,
        timeout=30
    )
    response.raise_for_status()

    run_suffix = f" - Run {run_id}"
    for issue in response.json():
        if (
            "pull_request" not in issue
            and issue.get("title", "").endswith(run_suffix)
        ):
            return issue

    return None

def publish_weekly_digest(top_issues_by_repo):
    parts = DIGEST_REPOSITORY.split("/")
    if len(parts) != 2 or not all(parts):
        raise ValueError(
            "DIGEST_REPOSITORY must use the 'owner/repository' format."
        )

    owner, repo = parts
    run_id = os.getenv("GITHUB_RUN_ID", "unknown")
    title = build_digest_title()
    existing_issue = find_existing_digest_issue(
        DIGEST_REPOSITORY,
        run_id
    )

    if existing_issue:
        print(
            f"Weekly digest issue already exists: "
            f"{existing_issue['html_url']}"
        )
        return existing_issue

    response = requests.post(
        f"{GH_REST_URL}/repos/{owner}/{repo}/issues",
        headers=headers,
        json={
            "title": title,
            "body": build_digest_body(top_issues_by_repo),
            "labels": DIGEST_LABELS
        },
        timeout=30
    )
    response.raise_for_status()

    digest_issue = response.json()
    print(f"Weekly digest published: {digest_issue['html_url']}")
    return digest_issue

# -------------------------------------------------------------------
# Execution Entry Point
# -------------------------------------------------------------------
def main():
    print(f"Connecting to GitHub Projects (v2) for organization '{ORGANIZATION_NAME}'...")
    print(f"Microsoft/Azure issue lookback starts: {ISSUE_LOOKBACK_TIMESTAMP[:10]}")
    print(f"Other repository issue lookback starts: {SINCE_TWO_YEARS_TIMESTAMP[:10]}")
    print(f"Filtering issues with >= {MIN_RECENT_COMMENTS} comments since: {SINCE_TWO_YEARS_TIMESTAMP[:10]}")
    top_issues_by_repo = {}
    project_id, score_field_id, repo_field_id = get_project_and_field_ids()

    # Step A: Clear board
    clear_project_board(project_id)

    # Step B: Populate fresh issues
    for target in TARGET_REPOS:
        print(f"Processing External Repository: {target}")
        issues = fetch_external_repo_issues(target)
        
        if not issues:
            top_issues_by_repo[target] = []
            print(f"  └─ No matching issues found.")
            continue

        scored_issues = [
            (issue, compute_priority_score(issue))
            for issue in issues
        ]
        scored_issues.sort(
            key=lambda scored_issue: scored_issue[1],
            reverse=True
        )
        top_issues_by_repo[target] = scored_issues[:3]

        for issue, score in scored_issues[:MAX_PROJECT_ISSUES_PER_REPO]:
            repo_name = issue["repo_full_name"]
            sync_to_github_project(project_id, score_field_id, repo_field_id, issue["id"], repo_name, score)
            print(f"  └─ [ADDED] Issue #{issue['number']} ({repo_name}) -> Priority Score: {score}")

    publish_digest = (
        os.getenv("PUBLISH_WEEKLY_DIGEST", "").strip().lower() == "true"
    )
    if publish_digest:
        publish_weekly_digest(top_issues_by_repo)

    try:
        send_success_email()
    except (smtplib.SMTPException, OSError, RuntimeError, ValueError) as error:
        print(f"Warning: Success email notification failed: {error}")

if __name__ == "__main__":
    main()