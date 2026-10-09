import os
import sys
import time
from datetime import datetime, timedelta, timezone
import requests

# -------------------------------------------------------------------
# Configuration & Environment Setup
# -------------------------------------------------------------------
GH_TOKEN = os.getenv("GITHUB_APP_TOKEN") or os.getenv("GH_PAT")
ORGANIZATION_NAME = os.getenv("ORGANIZATION_NAME", "msftjonw-labs")
PROJECT_NUMBER = int(os.getenv("PROJECT_NUMBER", "1"))
SCORE_FIELD_NAME = os.getenv("CUSTOM_FIELD_NAME", "Priority Score")
REPO_FIELD_NAME = os.getenv("REPO_FIELD_NAME", "Source")

if not GH_TOKEN:
    print("Error: Missing required environment variable GITHUB_APP_TOKEN.", file=sys.stderr)
    sys.exit(1)

TARGET_REPOS_RAW = os.getenv("TARGET_REPOS", "")
TARGET_REPOS = [r.strip() for r in TARGET_REPOS_RAW.split(",") if r.strip()]

# Activity Threshold Configuration
MIN_RECENT_COMMENTS = int(os.getenv("MIN_RECENT_COMMENTS", "2"))

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
MAX_ISSUES_PER_REPO = 100

# 730-day (2-year) cutoff timestamp for active discussion check
TWO_YEARS_AGO = datetime.now(timezone.utc) - timedelta(days=730)
SINCE_TWO_YEARS_TIMESTAMP = TWO_YEARS_AGO.isoformat()

MAX_GRAPHQL_ATTEMPTS = 3
TRANSIENT_STATUS_CODES = {500, 502, 503, 504}
TRANSIENT_ERROR_MARKERS = ("something went wrong while executing your query", "timeout")
ISSUE_PAGE_SIZE = 10
MAX_ISSUES_PER_REPO = 100

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
    data = run_graphql(query, {"projectId": project_id})
    items = data.get("node", {}).get("items", {}).get("nodes", [])
    
    if not items:
        print("Board is already empty.")
        return

    print(f"Clearing {len(items)} existing items from Project board...")
    
    delete_mutation = """
    mutation($projectId: ID!, $itemId: ID!) {
      deleteProjectV2Item(input: {projectId: $projectId, itemId: $itemId}) {
        deletedItemId
      }
    }
    """
    for item in items:
        run_graphql(delete_mutation, {"projectId": project_id, "itemId": item["id"]})
    print("Project board pre-clearing complete.\n")

# -------------------------------------------------------------------
# 2. Label Resolution & Fetching (730-Day Activity Window)
# -------------------------------------------------------------------
def get_required_label_for_repo(repo_full_name):
    repo_lower = repo_full_name.lower()

    no_label_repos = [
        "microsoft/windows-containers", 
        "microsoft/windows-container-tools",
        "docker/for-win"
    ]
    if repo_lower in no_label_repos:
        return None
    elif repo_lower == "azure/aks":
        return "windows"
    elif repo_lower in ["kubernetes/kubernetes", "kubernetes/enhancements", "kubernetes/community"]:
        return "sig/windows"
    elif repo_lower in ["moby/moby", "containerd/containerd"]:
        return "platform/windows"
    else:
        return "sig/windows"

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
    
    url = f"{GH_REST_URL}/repos/{owner}/{repo}/issues"
    params = {
        "state": "open",
        "per_page": 100,
        "sort": "updated",
        "direction": "desc",
        "since": SINCE_TWO_YEARS_TIMESTAMP
    }

    # Remove rigid REST label parameter for Azure/AKS to handle varied label conventions client-side
    if required_label and repo_full_name.lower() != "azure/aks":
        print(f"  └─ Exact Label Requirement: '{required_label}' | Activity Threshold: >= {MIN_RECENT_COMMENTS} comments in past 730 days")
        params["labels"] = required_label
    else:
        print(f"  └─ Fetching Open Issues | Activity Threshold: >= {MIN_RECENT_COMMENTS} comments in past 730 days")

    response = requests.get(url, headers=headers, params=params)
    if response.status_code != 200:
        print(f"  └─ Failed to fetch issues from REST API ({response.status_code}): {response.text}")
        return []

    raw_issues = response.json()
    verified_issues = []
    fetched = 0
    after = None

    for issue in raw_issues:
        if "pull_request" in issue:
            continue

        if len(verified_issues) >= MAX_ISSUES_PER_REPO:
            break

        label_names = [l["name"].strip().lower() for l in issue.get("labels", []) if isinstance(l, dict) and "name" in l]
        
        # Label matching verification
        if repo_full_name.lower() == "azure/aks":
            if not any("win" in l for l in label_names):
                continue
        elif required_label:
            target = required_label.strip().lower()
            if target not in label_names:
                print(f"  └─ [EXCLUDED - LABEL MISMATCH] Issue #{issue['number']} missing exact label '{required_label}'")
                continue

        gql_details = fetch_issue_graphql_details(issue["node_id"])
        if not gql_details:
            continue

        comments = gql_details.get("comments", {}).get("nodes", [])
        
        # Count comments created in the past 730 days (2 years)
        recent_comments_count = 0
        for comment in comments:
            if comment and comment.get("createdAt"):
                comment_dt = datetime.fromisoformat(comment["createdAt"].replace("Z", "+00:00"))
                if comment_dt >= TWO_YEARS_AGO:
                    recent_comments_count += 1

        # Activity Filter Check against 730-day window
        if recent_comments_count < MIN_RECENT_COMMENTS:
            print(f"  └─ [EXCLUDED - INACTIVE] Issue #{issue['number']} has {recent_comments_count} comment(s) in past 730 days (Threshold: >= {MIN_RECENT_COMMENTS})")
            continue

        gql_details["raw_label_names"] = label_names
        gql_details["recent_comments_count"] = recent_comments_count
        gql_details["repo_full_name"] = repo_full_name
        verified_issues.append(gql_details)

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
# Execution Entry Point
# -------------------------------------------------------------------
def main():
    print(f"Connecting to GitHub Projects (v2) for organization '{ORGANIZATION_NAME}'...")
    print(f"Filtering issues with >= {MIN_RECENT_COMMENTS} comments since: {SINCE_TWO_YEARS_TIMESTAMP[:10]}")
    project_id, score_field_id, repo_field_id = get_project_and_field_ids()

    # Step A: Clear board
    clear_project_board(project_id)

    # Step B: Populate fresh issues
    for target in TARGET_REPOS:
        print(f"Processing External Repository: {target}")
        issues = fetch_external_repo_issues(target)
        
        if not issues:
            print(f"  └─ No matching issues found.")
            continue

        for issue in issues:
            score = compute_priority_score(issue)
            repo_name = issue["repo_full_name"]
            sync_to_github_project(project_id, score_field_id, repo_field_id, issue["id"], repo_name, score)
            print(f"  └─ [ADDED] Issue #{issue['number']} ({repo_name}) -> Priority Score: {score}")

if __name__ == "__main__":
    main()