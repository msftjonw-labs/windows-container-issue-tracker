import os
import sys
import time
import requests

# -------------------------------------------------------------------
# Configuration & Environment Setup
# -------------------------------------------------------------------
GH_TOKEN = os.getenv("GITHUB_APP_TOKEN") or os.getenv("GH_PAT")
ORGANIZATION_NAME = os.getenv("ORGANIZATION_NAME", "msftjonw-labs")
PROJECT_NUMBER = int(os.getenv("PROJECT_NUMBER", "1"))
FIELD_NAME = os.getenv("CUSTOM_FIELD_NAME", "Priority Score")

if not GH_TOKEN:
    print("Error: Missing required environment variable GITHUB_APP_TOKEN.", file=sys.stderr)
    sys.exit(1)

TARGET_REPOS_RAW = os.getenv("TARGET_REPOS", "")
TARGET_REPOS = [r.strip() for r in TARGET_REPOS_RAW.split(",") if r.strip()]

# Custom Scoring Weights
WEIGHT_UNIQUE_USERS = 3.0
WEIGHT_TOTAL_COMMENTS = 1.0
LABEL_WEIGHTS = {
    "bug": 5.0,
    "customer-reported": 10.0,
    "p1": 15.0,
    "p2": 8.0,
    "feature-request": 2.0
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
    field_id = None
    for field in project["fields"]["nodes"]:
        if field and field.get("name") == FIELD_NAME:
            field_id = field["id"]
            break
            
    if not field_id:
        raise ValueError(f"Custom field '{FIELD_NAME}' not found in Project #{PROJECT_NUMBER}")
        
    return project_id, field_id

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
# 2. Strict Exact-Label Fetching via GitHub REST API
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
    """
    Fetches GraphQL node details (comments and node ID) needed for scoring & project sync.
    """
    query = """
    query($id: ID!) {
      node(id: $id) {
        ... on Issue {
          id
          number
          title
          url
          comments(first: 100) {
            nodes {
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
    parts = repo_full_name.split("/")
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
        "direction": "desc"
    }

    if required_label:
        print(f"  └─ Strict Label Requirement: REST API exact label parameter '{required_label}'")
        params["labels"] = required_label
    else:
        print(f"  └─ Fetching All Open Issues (No Label Filter)")

    response = requests.get(url, headers=headers, params=params)
    if response.status_code != 200:
        print(f"  └─ Failed to fetch issues from REST API ({response.status_code}): {response.text}")
        return []

    raw_issues = response.json()
    verified_issues = []

    for issue in raw_issues:
        # Ignore Pull Requests (GitHub REST API includes PRs in the issues endpoint)
        if "pull_request" in issue:
            continue

        if len(verified_issues) >= MAX_ISSUES_PER_REPO:
            break

        # Double Check Exact Match against raw issue label names
        label_names = [l["name"].strip().lower() for l in issue.get("labels", []) if isinstance(l, dict) and "name" in l]
        
        if required_label:
            target = required_label.strip().lower()
            if target not in label_names:
                print(f"  └─ [EXCLUDED] Issue #{issue['number']} missing exact label '{required_label}' (Labels: {label_names})")
                continue

        # Enrich issue with GraphQL details (comments & node_id)
        gql_details = fetch_issue_graphql_details(issue["node_id"])
        if not gql_details:
            continue

        # Attach raw labels from REST payload to ensure scoring reads full set
        gql_details["raw_label_names"] = label_names
        verified_issues.append(gql_details)

    return verified_issues

# -------------------------------------------------------------------
# 3. Custom Weighted Priority Calculation
# -------------------------------------------------------------------
def compute_priority_score(issue):
    comments = issue.get("comments", {}).get("nodes", [])
    
    authors = {c["author"]["login"] for c in comments if c and c.get("author")}
    unique_user_count = len(authors)
    
    total_comments = len(comments)
    
    # Read label names populated from REST API response
    label_names = issue.get("raw_label_names", [])
    label_score = sum(LABEL_WEIGHTS.get(label, 0.0) for label in label_names)
    
    final_score = (
        (unique_user_count * WEIGHT_UNIQUE_USERS) +
        (total_comments * WEIGHT_TOTAL_COMMENTS) +
        label_score
    )
    
    return round(final_score, 2)

# -------------------------------------------------------------------
# 4. Write Item & Score into Organization GitHub Project v2 Board
# -------------------------------------------------------------------
def sync_to_github_project(project_id, field_id, issue_node_id, score):
    add_item_mutation = """
    mutation($projectId: ID!, $contentId: ID!) {
      addProjectV2ItemById(input: {projectId: $projectId, contentId: $contentId}) {
        item { id }
      }
    }
    """
    item_data = run_graphql(add_item_mutation, {"projectId": project_id, "contentId": issue_node_id})
    item_id = item_data["addProjectV2ItemById"]["item"]["id"]

    update_field_mutation = """
    mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!, $value: Float!) {
      updateProjectV2ItemFieldValue(
        input: {
          projectId: $projectId
          itemId: $itemId
          fieldId: $fieldId
          value: { number: $value }
        }
      ) {
        projectV2Item { id }
      }
    }
    """
    run_graphql(update_field_mutation, {
        "projectId": project_id,
        "itemId": item_id,
        "fieldId": field_id,
        "value": float(score)
    })

# -------------------------------------------------------------------
# Execution Entry Point
# -------------------------------------------------------------------
def main():
    print(f"Connecting to GitHub Projects (v2) for organization '{ORGANIZATION_NAME}'...")
    project_id, field_id = get_project_and_field_ids()

    # Step A: Pre-clear board
    clear_project_board(project_id)

    # Step B: Populate fresh, strictly filtered issues
    for target in TARGET_REPOS:
        print(f"Processing External Repository: {target}")
        issues = fetch_external_repo_issues(target)
        
        if not issues:
            print(f"  └─ No matching issues found.")
            continue

        for issue in issues:
            score = compute_priority_score(issue)
            sync_to_github_project(project_id, field_id, issue["id"], score)
            print(f"  └─ [ADDED] Issue #{issue['number']} ('{issue['title'][:30]}...') -> Priority Score: {score}")

if __name__ == "__main__":
    main()
