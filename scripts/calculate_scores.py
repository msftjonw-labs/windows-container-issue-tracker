import os
import sys
import time
import requests

# -----------------------------------------------------------------------------
# Configuration & Environment Setup
# -----------------------------------------------------------------------------
GITHUB_APP_TOKEN = os.getenv("GITHUB_APP_TOKEN")
ORGANIZATION_NAME = os.getenv("ORGANIZATION_NAME", "msftjonw-labs")
PROJECT_NUMBER = int(os.getenv("PROJECT_NUMBER", "1"))
CUSTOM_FIELD_NAME = os.getenv("CUSTOM_FIELD_NAME", "Priority Score")
TARGET_REPOS_RAW = os.getenv("TARGET_REPOS", "")

if not GITHUB_APP_TOKEN:
    print("Error: Missing required environment variable GITHUB_APP_TOKEN.", file=sys.stderr)
    sys.exit(1)

# Clean up repo list
TARGET_REPOS = [r.strip() for r in TARGET_REPOS_RAW.replace("\n", "").split(",") if r.strip()]

GRAPHQL_URL = "https://api.github.com/graphql"
HEADERS = {
    "Authorization": f"Bearer {GITHUB_APP_TOKEN}",
    "Accept": "application/vnd.github+json"
}

# -----------------------------------------------------------------------------
# GraphQL Helper with Exponential Backoff
# -----------------------------------------------------------------------------
def run_graphql(query: str, variables: dict = None, max_retries: int = 5) -> dict:
    delay = 2
    for attempt in range(1, max_retries + 1):
        try:
            response = requests.post(
                GRAPHQL_URL,
                json={"query": query, "variables": variables or {}},
                headers=HEADERS,
                timeout=60
            )
            
            if response.status_code in (502, 503, 504):
                print(f"[Warning] HTTP {response.status_code} received. Retrying in {delay}s (Attempt {attempt}/{max_retries})...")
                time.sleep(delay)
                delay *= 2
                continue

            response.raise_for_status()
            res_json = response.json()

            if "errors" in res_json and not res_json.get("data"):
                raise Exception(f"GraphQL Errors: {res_json['errors']}")

            return res_json.get("data", {})

        except (requests.exceptions.RequestException, Exception) as e:
            if attempt == max_retries:
                raise Exception(f"GraphQL query failed after {max_retries} attempts: {e}")
            print(f"[Warning] Network error: {e}. Retrying in {delay}s (Attempt {attempt}/{max_retries})...")
            time.sleep(delay)
            delay *= 2

# -----------------------------------------------------------------------------
# Organization Project v2 Metadata Resolution
# -----------------------------------------------------------------------------
def get_org_project_details(org_name: str, proj_num: int, field_name: str):
    """Fetches Project v2 ID and custom Priority Score field ID for an Organization."""
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
    data = run_graphql(query, {"org": org_name, "number": proj_num})
    org_data = data.get("organization")
    if not org_data:
        raise ValueError(f"Organization '{org_name}' not found or token lacks access.")
    
    project = org_data.get("projectV2")
    if not project:
        raise ValueError(f"Project #{proj_num} not found under organization '{org_name}'.")

    project_id = project["id"]
    field_id = None

    for node in project["fields"]["nodes"]:
        if node and node.get("name") == field_name:
            field_id = node.get("id")
            break

    if not field_id:
        raise ValueError(f"Custom field '{field_name}' not found on Project #{proj_num}.")

    return project_id, field_id

# -----------------------------------------------------------------------------
# Fetch Issues from Target Repositories (Optimized Payloads)
# -----------------------------------------------------------------------------
def fetch_repo_issues(owner: str, repo: str):
    """Fetches open issues using lightweight paginated payloads to avoid HTTP 504 timeouts."""
    query = """
    query($owner: String!, $repo: String!, $cursor: String) {
      repository(owner: $owner, name: $repo) {
        issues(first: 35, states: OPEN, after: $cursor, orderBy: {field: UPDATED_AT, direction: DESC}) {
          pageInfo {
            hasNextPage
            endCursor
          }
          nodes {
            id
            number
            title
            url
            comments {
              totalCount
            }
            reactions {
              totalCount
            }
            labels(first: 20) {
              nodes {
                name
              }
            }
          }
        }
      }
    }
    """
    issues = []
    cursor = None
    has_next = True

    while has_next:
        data = run_graphql(query, {"owner": owner, "repo": repo, "cursor": cursor})
        repo_data = data.get("repository")
        if not repo_data:
            print(f"[Warning] Could not access repository {owner}/{repo}. Skipping.")
            break

        issue_conn = repo_data["issues"]
        issues.extend(issue_conn["nodes"])

        has_next = issue_conn["pageInfo"]["hasNextPage"]
        cursor = issue_conn["pageInfo"]["endCursor"]

    return issues

# -----------------------------------------------------------------------------
# Scoring Logic
# -----------------------------------------------------------------------------
def calculate_priority_score(issue: dict) -> float:
    """Calculates priority score based on engagement metrics and labels."""
    comments_count = issue["comments"]["totalCount"]
    reactions_count = issue["reactions"]["totalCount"]
    labels = [l["name"].lower() for l in issue["labels"]["nodes"]]

    score = (comments_count * 1.5) + (reactions_count * 2.0)

    if any(l in labels for l in ["bug", "kind/bug", "type/bug"]):
        score += 5.0
    if any(l in labels for l in ["critical", "priority/critical-urgent", "P0"]):
        score += 10.0

    return round(score, 2)

# -----------------------------------------------------------------------------
# Sync to Organization Project v2 Board
# -----------------------------------------------------------------------------
def sync_issue_to_project(project_id: str, field_id: str, content_id: str, score: float):
    """Adds issue to org Project v2 and sets the numerical Priority Score."""
    # 1. Add item to Project v2
    add_item_query = """
    mutation($projectId: ID!, $contentId: ID!) {
      addProjectV2ItemById(input: {projectId: $projectId, contentId: $contentId}) {
        item {
          id
        }
      }
    }
    """
    data = run_graphql(add_item_query, {"projectId": project_id, "contentId": content_id})
    item_id = data["addProjectV2ItemById"]["item"]["id"]

    # 2. Update custom score field value
    update_field_query = """
    mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!, $value: Float!) {
      updateProjectV2ItemFieldValue(
        input: {
          projectId: $projectId
          itemId: $itemId
          fieldId: $fieldId
          value: { number: $value }
        }
      ) {
        projectV2Item {
          id
        }
      }
    }
    """
    run_graphql(update_field_query, {
        "projectId": project_id,
        "itemId": item_id,
        "fieldId": field_id,
        "value": score
    })

# -----------------------------------------------------------------------------
# Main Execution Pipeline
# -----------------------------------------------------------------------------
def main():
    print(f"Resolving Organization Project v2 metadata for '{ORGANIZATION_NAME}'...")
    project_id, field_id = get_org_project_details(ORGANIZATION_NAME, PROJECT_NUMBER, CUSTOM_FIELD_NAME)
    print(f"Project ID: {project_id} | Field ID: {field_id}")

    total_processed = 0

    for repo_full_name in TARGET_REPOS:
        parts = repo_full_name.split("/")
        if len(parts) != 2:
            print(f"Skipping invalid repo string: '{repo_full_name}'")
            continue

        owner, repo = parts[0], parts[1]
        print(f"\nProcessing issues for {owner}/{repo}...")

        try:
            issues = fetch_repo_issues(owner, repo)
            print(f"Found {len(issues)} open issues in {owner}/{repo}.")

            for issue in issues:
                score = calculate_priority_score(issue)
                sync_issue_to_project(project_id, field_id, issue["id"], score)
                total_processed += 1
                
                # Small delay to prevent API rate limit abuse penalties
                time.sleep(0.1)

        except Exception as err:
            print(f"[Error] Failed processing repo {owner}/{repo}: {err}")

    print(f"\nSuccessfully scored and synced {total_processed} total issues across repositories.")

if __name__ == "__main__":
    main()