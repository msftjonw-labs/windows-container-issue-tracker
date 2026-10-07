import os
import sys
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

def run_graphql(query, variables=None):
    response = requests.post(
        GH_GRAPHQL_URL, 
        json={"query": query, "variables": variables}, 
        headers=headers
    )
    if response.status_code != 200:
        raise Exception(f"GraphQL query failed ({response.status_code}): {response.text}")
    res_data = response.json()
    if "errors" in res_data:
        raise Exception(f"GraphQL Errors: {res_data['errors']}")
    return res_data["data"]

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
# 2. Server-Side Direct Label Filtering Query
# -------------------------------------------------------------------
def get_required_label_for_repo(repo_full_name):
    """
    Returns exact label string required or None if no label filter is needed.
    """
    repo_lower = repo_full_name.lower()

    # Rule 1: No label filter
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
        print(f"  └─ Applying Server-Side Label Filter: labels=['{required_label}']")
        labels_param = [required_label]
    else:
        print(f"  └─ Fetching All Open Issues (No Label Filter)")
        labels_param = None

    # Query repository issues passing the 'labels' argument directly to GitHub
    query = """
    query($owner: String!, $repo: String!, $labels: [String!]) {
      repository(owner: $owner, name: $repo) {
        issues(states: OPEN, labels: $labels, first: 100, orderBy: {field: UPDATED_AT, direction: DESC}) {
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
    
    variables = {
        "owner": owner,
        "repo": repo,
        "labels": labels_param
    }
    
    data = run_graphql(query, variables)
    
    if not data or not data.get("repository") or not data["repository"].get("issues"):
        print(f"  └─ No matching issues found.")
        return []

    raw_nodes = data["repository"]["issues"]["nodes"]
    
    # Strictly check Python array as a secondary safety guard
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
            if required_label.lower() not in node_labels:
                # Discard if truncated label nodes didn't include it
                continue

        verified_issues.append(node)

    return verified_issues

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
    print(f"Connecting to personal GitHub Projects (v2) for user '{GITHUB_USER}'...")
    project_id, field_id = get_project_and_field_ids()

    for target in TARGET_REPOS:
        print(f"\nProcessing External Repository: {target}")
        issues = fetch_external_repo_issues(target)
        
        if not issues:
            print(f"  └─ No open issues matching the required label.")
            continue

        for issue in issues:
            score = compute_priority_score(issue)
            sync_to_github_project(project_id, field_id, issue["id"], score)
            print(f"  └─ Issue #{issue['number']} ('{issue['title'][:30]}...') -> Priority Score: {score}")

if __name__ == "__main__":
    main()