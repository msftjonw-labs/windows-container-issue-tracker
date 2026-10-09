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

# API Client Setup
GH_GRAPHQL_URL = "https://api.github.com/graphql"
headers = {"Authorization": f"Bearer {GH_TOKEN}"}

MAX_GRAPHQL_ATTEMPTS = 3
TRANSIENT_STATUS_CODES = {500, 502, 503, 504}
TRANSIENT_ERROR_MARKERS = ("something went wrong while executing your query", "timeout")
ISSUE_PAGE_SIZE = 10
MAX_ISSUES_PER_REPO = 100

def run_graphql(query, variables=None):
    for attempt in range(1, MAX_GRAPHQL_ATTEMPTS + 1):
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
        if not transient or attempt == MAX_GRAPHQL_ATTEMPTS:
            raise error
        delay = 2 ** (attempt - 1)
        print(f"  └─ Transient GraphQL failure (attempt {attempt}/{MAX_GRAPHQL_ATTEMPTS}); retrying in {delay}s...")
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
# 2. Strict Label Mapping & Direct Search API Querying
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

def fetch_external_repo_issues(repo_full_name):
    parts = repo_full_name.split("/")
    if len(parts) != 2:
        print(f"Skipping invalid target format '{repo_full_name}'. Expected 'owner/repo'.")
        return []

    required_label = get_required_label_for_repo(repo_full_name)
    
    if required_label:
        print(f"  └─ Strict Label Requirement: Exact search query for label:\"{required_label}\"")
        # Exact quote label search in GitHub Search API prevents token splitting
        search_query = f'repo:{repo_full_name} is:issue is:open label:"{required_label}"'
    else:
        print(f"  └─ Fetching All Open Issues (No Label Filter)")
        search_query = f'repo:{repo_full_name} is:issue is:open'

    graphql_query = """
    query($searchQuery: String!, $first: Int!, $after: String) {
      search(query: $searchQuery, type: ISSUE, first: $first, after: $after) {
        issueCount
        nodes {
          ... on Issue {
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
        pageInfo {
          hasNextPage
          endCursor
        }
      }
    }
    """

    verified_issues = []
    fetched = 0
    after = None

    while fetched < MAX_ISSUES_PER_REPO:
        data = run_graphql(graphql_query, {"searchQuery": search_query, "first": ISSUE_PAGE_SIZE, "after": after})

        if not data or not data.get("search") or not data["search"].get("nodes"):
            if fetched == 0:
                print(f"  └─ No matching issues found.")
            break

        search_result = data["search"]
        raw_nodes = search_result.get("nodes") or []
        fetched += len(raw_nodes)

        for node in raw_nodes:
            if not node or "id" not in node:
                continue

            # Strict secondary verification on returned issue labels
            if required_label:
                raw_labels = [
                    l["name"].strip().lower()
                    for l in node.get("labels", {}).get("nodes", [])
                    if l and "name" in l
                ]
                target_label = required_label.strip().lower()

                # Double check that target label is an exact whole-string match in raw labels
                if not any(label == target_label for label in raw_labels):
                    print(f"  └─ [EXCLUDED] Issue #{node.get('number')} failed exact string check (Found: {raw_labels})")
                    continue

            verified_issues.append(node)

        page_info = search_result.get("pageInfo") or {}
        if not page_info.get("hasNextPage") or not page_info.get("endCursor"):
            break
        after = page_info["endCursor"]

    return verified_issues

# -------------------------------------------------------------------
# 3. Custom Weighted Priority Calculation
# -------------------------------------------------------------------
def compute_priority_score(issue):
    comments = issue.get("comments", {}).get("nodes", [])
    
    authors = {c["author"]["login"] for c in comments if c and c.get("author")}
    unique_user_count = len(authors)
    
    total_comments = len(comments)
    
    labels = issue.get("labels", {}).get("nodes", [])
    label_names = [l["name"].lower() for l in labels if l and "name" in l]
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
