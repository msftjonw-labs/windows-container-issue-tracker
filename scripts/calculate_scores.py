import os
import sys
import time
import requests

# -------------------------------------------------------------------
# Configuration & Environment Setup
# -------------------------------------------------------------------
# Dual Tokens: App Token for public repos, Personal PAT for User Project v2
APP_READ_TOKEN = os.getenv("APP_PUBLIC_READ_TOKEN")
PROJECT_PAT = os.getenv("PERSONAL_PROJECT_PAT")

GITHUB_USER = os.getenv("ORGANIZATION_NAME")  # Your personal GitHub username
PROJECT_NUMBER = int(os.getenv("PROJECT_NUMBER", "1"))
FIELD_NAME = os.getenv("CUSTOM_FIELD_NAME", "Priority Score")

# Validate credentials on startup
if not APP_READ_TOKEN:
    raise ValueError("Missing required environment variable: APP_PUBLIC_READ_TOKEN")
if not PROJECT_PAT:
    raise ValueError("Missing required environment variable: PERSONAL_PROJECT_PAT")

# Parse target external repos ("owner1/repo1, owner2/repo2")
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

# API Client Setup
GH_GRAPHQL_URL = "https://api.github.com/graphql"

headers_public = {
    "Authorization": f"Bearer {APP_READ_TOKEN}",
    "Accept": "application/vnd.github+json"
}

headers_project = {
    "Authorization": f"Bearer {PROJECT_PAT}",
    "Accept": "application/vnd.github+json"
}

def run_public_graphql(query, variables=None, max_retries=5, backoff_factor=3):
    """
    Executes GraphQL queries against public repositories using the GitHub App token.
    Retries automatically on HTTP 500/502/503/504 transient errors up to max_retries.
    Uses a 60-second request timeout and exponential backoff.
    """
    for attempt in range(1, max_retries + 1):
        try:
            response = requests.post(
                GH_GRAPHQL_URL, 
                json={"query": query, "variables": variables}, 
                headers=headers_public,
                timeout=60
            )
            
            # Retry on 504 Gateway Timeout and server error statuses
            if response.status_code in [500, 502, 503, 504]:
                if attempt < max_retries:
                    sleep_time = backoff_factor ** attempt
                    print(f"  └─ [WARNING] Public GraphQL HTTP {response.status_code}. Retrying ({attempt}/{max_retries}) in {sleep_time}s...")
                    time.sleep(sleep_time)
                    continue

            if response.status_code != 200:
                raise Exception(f"Public Repo GraphQL query failed ({response.status_code}): {response.text}")
            
            res_data = response.json()
            if "errors" in res_data:
                raise Exception(f"Public Repo GraphQL Errors: {res_data['errors']}")
            
            return res_data["data"]

        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            if attempt < max_retries:
                sleep_time = backoff_factor ** attempt
                print(f"  └─ [WARNING] Public GraphQL connection error: {e}. Retrying ({attempt}/{max_retries}) in {sleep_time}s...")
                time.sleep(sleep_time)
            else:
                raise Exception(f"Public Repo GraphQL failed after {max_retries} attempts due to network timeout/error: {e}")

def run_project_graphql(query, variables=None, max_retries=3, backoff_factor=2):
    """
    Executes GraphQL queries against your personal Project v2 using your Personal PAT.
    Includes retry logic for transient errors.
    """
    for attempt in range(1, max_retries + 1):
        try:
            response = requests.post(
                GH_GRAPHQL_URL, 
                json={"query": query, "variables": variables}, 
                headers=headers_project,
                timeout=30
            )
            
            if response.status_code in [500, 502, 503, 504]:
                if attempt < max_retries:
                    sleep_time = backoff_factor ** attempt
                    print(f"  └─ [WARNING] Project GraphQL HTTP {response.status_code}. Retrying ({attempt}/{max_retries}) in {sleep_time}s...")
                    time.sleep(sleep_time)
                    continue

            if response.status_code != 200:
                raise Exception(f"Project v2 GraphQL query failed ({response.status_code}): {response.text}")
            
            res_data = response.json()
            if "errors" in res_data:
                raise Exception(f"Project v2 GraphQL Errors: {res_data['errors']}")
            
            return res_data["data"]

        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            if attempt < max_retries:
                sleep_time = backoff_factor ** attempt
                print(f"  └─ [WARNING] Project GraphQL connection error: {e}. Retrying ({attempt}/{max_retries}) in {sleep_time}s...")
                time.sleep(sleep_time)
            else:
                raise Exception(f"Project v2 GraphQL failed after {max_retries} attempts due to network timeout/error: {e}")

# -------------------------------------------------------------------
# 1. Personal GitHub Project v2 Discovery & Board Cleanup
# -------------------------------------------------------------------
def get_project_and_field_ids():
    query = """
    query($user: String!, $number: Int!) {
      user(login: $user) {
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
    data = run_project_graphql(query, {"user": GITHUB_USER, "number": PROJECT_NUMBER})
    
    user_data = data.get("user")
    if not user_data:
        raise ValueError(f"GitHub user '{GITHUB_USER}' not found.")
        
    project = user_data.get("projectV2")
    if not project:
        raise ValueError(f"Project #{PROJECT_NUMBER} not found under personal account '{GITHUB_USER}'")
        
    project_id = project["id"]
    field_id = None
    for field in project["fields"]["nodes"]:
        if field.get("name") == FIELD_NAME:
            field_id = field["id"]
            break
            
    if not field_id:
        raise ValueError(f"Custom field '{FIELD_NAME}' not found in Project #{PROJECT_NUMBER}")
        
    return project_id, field_id

def clear_project_board(project_id):
    """
    Fetches all items currently on the project board and deletes them.
    """
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
    data = run_project_graphql(query, {"projectId": project_id})
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
        run_project_graphql(delete_mutation, {"projectId": project_id, "itemId": item["id"]})
    print("Project board pre-clearing complete.\n")

# -------------------------------------------------------------------
# 2. Direct Repository Issue Fetching & Exact String Label Validation
# -------------------------------------------------------------------
def get_required_label_for_repo(repo_full_name):
    """
    Returns exact label string required or None if no label filter is needed.
    """
    repo_lower = repo_full_name.lower()

    # Rule 1: No label requirement
    no_label_repos = [
        "microsoft/windows-containers", 
        "microsoft/windows-container-tools",
        "docker/for-win"
    ]
    if repo_lower in no_label_repos:
        return None
    
    # Rule 2: Must contain exact label 'windows'
    elif repo_lower == "azure/aks":
        return "windows"
    
    # Rule 3: Must contain exact label 'sig/windows'
    elif repo_lower in ["kubernetes/kubernetes", "kubernetes/enhancements", "kubernetes/community"]:
        return "sig/windows"
    
    # Rule 4: Must contain exact label 'platform/windows'
    elif repo_lower in ["moby/moby", "containerd/containerd"]:
        return "platform/windows"
    
    # Fallback
    else:
        return "sig/windows"

def fetch_external_repo_issues(repo_full_name):
    parts = repo_full_name.split("/")
    if len(parts) != 2:
        print(f"Skipping invalid target format '{repo_full_name}'. Expected 'owner/repo'.")
        return []

    owner, repo = parts[0], parts[1]
    required_label = get_required_label_for_repo(repo_full_name)
    
    if required_label:
        print(f"  └─ Strict Label Requirement: Exact match for '{required_label}'")
    else:
        print(f"  └─ Fetching All Open Issues (No Label Filter)")

    # Optimized Query Scope to Prevent HTTP 504 Timeouts
    query = """
    query($owner: String!, $repo: String!) {
      repository(owner: $owner, name: $repo) {
        issues(states: OPEN, first: 35, orderBy: {field: UPDATED_AT, direction: DESC}) {
          nodes {
            id
            number
            title
            url
            labels(first: 20) {
              nodes { name }
            }
            comments(first: 25) {
              totalCount
              nodes {
                author { login }
              }
            }
          }
        }
      }
    }
    """
    
    data = run_public_graphql(query, {"owner": owner, "repo": repo})
    
    if not data or not data.get("repository") or not data["repository"].get("issues"):
        print(f"  └─ No open issues found.")
        return []

    raw_nodes = data["repository"]["issues"]["nodes"]
    verified_issues = []

    for node in raw_nodes:
        if not node or "id" not in node:
            continue
            
        if required_label:
            node_labels = [
                l["name"].strip().lower() 
                for l in node.get("labels", {}).get("nodes", []) 
                if l and "name" in l
            ]
            
            target_label = required_label.strip().lower()
            
            if target_label not in node_labels:
                print(f"  └─ [EXCLUDED] Issue #{node['number']} missing exact label '{required_label}'")
                continue

        verified_issues.append(node)

    return verified_issues

# -------------------------------------------------------------------
# 3. Custom Weighted Priority Calculation
# -------------------------------------------------------------------
def compute_priority_score(issue):
    comments_obj = issue.get("comments", {})
    comments_nodes = comments_obj.get("nodes", [])
    
    # Metric A: Unique Commenters (from fetched sample)
    authors = {c["author"]["login"] for c in comments_nodes if c and c.get("author")}
    unique_user_count = len(authors)
    
    # Metric B: Total Comment Count (uses aggregate totalCount if available)
    total_comments = comments_obj.get("totalCount", len(comments_nodes))
    
    # Metric C: Label Weights
    labels = issue.get("labels", {}).get("nodes", [])
    label_names = [l["name"].lower() for l in labels if l and "name" in l]
    label_score = sum(LABEL_WEIGHTS.get(label, 0.0) for label in label_names)
    
    # Combined Formula
    final_score = (
        (unique_user_count * WEIGHT_UNIQUE_USERS) +
        (total_comments * WEIGHT_TOTAL_COMMENTS) +
        label_score
    )
    
    return round(final_score, 2)

# -------------------------------------------------------------------
# 4. Write Item & Score into your Personal GitHub Project v2 Board
# -------------------------------------------------------------------
def sync_to_github_project(project_id, field_id, issue_node_id, score):
    add_item_mutation = """
    mutation($projectId: ID!, $contentId: ID!) {
      addProjectV2ItemById(input: {projectId: $projectId, contentId: $contentId}) {
        item { id }
      }
    }
    """
    item_data = run_project_graphql(add_item_mutation, {"projectId": project_id, "contentId": issue_node_id})
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
    run_project_graphql(update_field_mutation, {
        "projectId": project_id,
        "itemId": item_id,
        "fieldId": field_id,
        "value": float(score)
    })

# -------------------------------------------------------------------
# Execution Entry Point
# -------------------------------------------------------------------
def main():
    print(f"Connecting to personal GitHub Projects (v2) for user '{GITHUB_USER}'...")
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