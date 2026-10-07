import os
import sys
import time
import requests

# -------------------------------------------------------------------
# Configuration & Environment Setup
# -------------------------------------------------------------------
GH_TOKEN = os.getenv("GH_PAT")
GITHUB_USER = os.getenv("ORGANIZATION_NAME")  # Your personal GitHub username
PROJECT_NUMBER = int(os.getenv("PROJECT_NUMBER", "1"))
FIELD_NAME = os.getenv("CUSTOM_FIELD_NAME", "Priority Score")

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
headers = {"Authorization": f"Bearer {GH_TOKEN}"}

MAX_GRAPHQL_ATTEMPTS = 3
TRANSIENT_HTTP_STATUS_CODES = {429, 500, 502, 503, 504}

def run_graphql(query, variables=None):
    for attempt in range(MAX_GRAPHQL_ATTEMPTS):
        try:
            response = requests.post(
                GH_GRAPHQL_URL,
                json={"query": query, "variables": variables},
                headers=headers
            )
        except requests.RequestException:
            if attempt == MAX_GRAPHQL_ATTEMPTS - 1:
                raise
        else:
            if response.status_code != 200:
                error = Exception(
                    f"GraphQL query failed ({response.status_code}): {response.text}"
                )
                if response.status_code not in TRANSIENT_HTTP_STATUS_CODES:
                    raise error
                if attempt == MAX_GRAPHQL_ATTEMPTS - 1:
                    raise error
            else:
                res_data = response.json()
                if "errors" not in res_data:
                    return res_data["data"]

                error = Exception(f"GraphQL Errors: {res_data['errors']}")
                is_transient = any(
                    "Something went wrong while executing your query."
                    in graphql_error.get("message", "")
                    for graphql_error in res_data["errors"]
                )
                if not is_transient or attempt == MAX_GRAPHQL_ATTEMPTS - 1:
                    raise error

        time.sleep(attempt + 1)

# -------------------------------------------------------------------
# 1. Personal GitHub Project v2 Field Discovery
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
    data = run_graphql(query, {"user": GITHUB_USER, "number": PROJECT_NUMBER})
    
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

# -------------------------------------------------------------------
# 2. Strict Repository-Level Querying & Exact Label Matching
# -------------------------------------------------------------------
def get_required_label_for_repo(repo_full_name):
    """
    Returns the exact required label string or None if no label filter is required.
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
        print(f"  └─ Strict Label Requirement: MUST EXACTLY MATCH '{required_label}'")
    else:
        print(f"  └─ No Label Requirement (Fetching open issues)")

    # Direct repository node query (bypasses GraphQL search engine tokenization issues)
    query = """
    query($owner: String!, $repo: String!, $first: Int!, $after: String) {
      repository(owner: $owner, name: $repo) {
        issues(states: OPEN, first: $first, after: $after, orderBy: {field: UPDATED_AT, direction: DESC}) {
          pageInfo {
            hasNextPage
            endCursor
          }
          nodes {
            id
            number
            title
            url
            labels(first: 50) {
              nodes { name }
            }
            comments(first: 100) {
              nodes {
                author { login }
              }
            }
          }
        }
      }
    }
    """
    raw_nodes = []
    cursor = None
    # Keep the 100-issue limit, but fetch smaller pages to avoid gateway timeouts.
    while len(raw_nodes) < 100:
        data = run_graphql(query, {
            "owner": owner,
            "repo": repo,
            "first": min(10, 100 - len(raw_nodes)),
            "after": cursor
        })

        if not data or not data.get("repository") or not data["repository"].get("issues"):
            print(f"Warning: Repository '{repo_full_name}' not found or has no open issues.")
            return []

        issues = data["repository"]["issues"]
        raw_nodes.extend(issues["nodes"])
        if not issues["nodes"] or not issues["pageInfo"]["hasNextPage"]:
            break
        cursor = issues["pageInfo"]["endCursor"]

    filtered_issues = []

    # Absolute exact string check on issue labels array
    for node in raw_nodes:
        if not node or "id" not in node:
            continue
        
        if required_label:
            # Extract exact label names from the node
            issue_labels = [
                l["name"].strip().lower() 
                for l in node.get("labels", {}).get("nodes", []) 
                if l and "name" in l
            ]
            
            target_label = required_label.strip().lower()
            
            # Reject if the required string is not explicitly inside the label array
            if target_label not in issue_labels:
                continue

        filtered_issues.append(node)

    return filtered_issues

# -------------------------------------------------------------------
# 3. Custom Weighted Priority Calculation
# -------------------------------------------------------------------
def compute_priority_score(issue):
    comments = issue.get("comments", {}).get("nodes", [])
    
    # Metric A: Unique Commenters
    authors = {c["author"]["login"] for c in comments if c and c.get("author")}
    unique_user_count = len(authors)
    
    # Metric B: Total Comment Count
    total_comments = len(comments)
    
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
    # Step A: Import External Issue Node into your Personal Project Board
    add_item_mutation = """
    mutation($projectId: ID!, $contentId: ID!) {
      addProjectV2ItemById(input: {projectId: $projectId, contentId: $contentId}) {
        item { id }
      }
    }
    """
    item_data = run_graphql(add_item_mutation, {"projectId": project_id, "contentId": issue_node_id})
    item_id = item_data["addProjectV2ItemById"]["item"]["id"]

    # Step B: Set Numerical Score in Custom Field
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
    print(f"Connecting to personal GitHub Projects (v2) for user '{GITHUB_USER}'...")
    project_id, field_id = get_project_and_field_ids()

    for target in TARGET_REPOS:
        print(f"\nProcessing External Repository: {target}")
        issues = fetch_external_repo_issues(target)
        
        if not issues:
            print(f"  └─ No matching issues found with required exact label.")
            continue

        for issue in issues:
            score = compute_priority_score(issue)
            sync_to_github_project(project_id, field_id, issue["id"], score)
            print(f"  └─ Issue #{issue['number']} ('{issue['title'][:30]}...') -> Priority Score: {score}")

if __name__ == "__main__":
    main()