import os
import sys
import requests

# -------------------------------------------------------------------
# Configuration & Environment Setup
# -------------------------------------------------------------------
GH_TOKEN = os.getenv("GH_PAT")
ORG_NAME = os.getenv("ORGANIZATION_NAME")  # Account/Org where YOUR Project v2 board resides
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
# 1. GitHub Project v2 Field Discovery
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
      user(login: $org) {
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
    data = run_graphql(query, {"org": ORG_NAME, "number": PROJECT_NUMBER})
    
    # Resolves whether ORGANIZATION_NAME is an Org or a Personal User Account
    project = (data.get("organization") or {}).get("projectV2") or (data.get("user") or {}).get("projectV2")
    if not project:
        raise ValueError(f"Project #{PROJECT_NUMBER} not found under account/org '{ORG_NAME}'")
        
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
# 2. Fetch Issues & Comments from External Target Repo
# -------------------------------------------------------------------
def fetch_external_repo_issues(repo_full_name):
    parts = repo_full_name.split("/")
    if len(parts) != 2:
        print(f"Skipping invalid target format '{repo_full_name}'. Expected 'owner/repo'.")
        return []
    
    owner, repo = parts[0], parts[1]

    query = """
    query($owner: String!, $repo: String!) {
      repository(owner: $owner, name: $repo) {
        issues(states: OPEN, first: 50, orderBy: {field: UPDATED_AT, direction: DESC}) {
          nodes {
            id
            number
            title
            url
            labels(first: 20) {
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
    data = run_graphql(query, {"owner": owner, "repo": repo})
    
    if not data or not data.get("repository"):
        print(f"Warning: Repository '{owner}/{repo}' not found or inaccessible.")
        return []

    return data["repository"]["issues"]["nodes"]

# -------------------------------------------------------------------
# 3. Custom Weighted Priority Calculation
# -------------------------------------------------------------------
def compute_priority_score(issue):
    comments = issue["comments"]["nodes"]
    
    # Metric A: Unique Commenters
    authors = {c["author"]["login"] for c in comments if c.get("author")}
    unique_user_count = len(authors)
    
    # Metric B: Total Comment Count
    total_comments = len(comments)
    
    # Metric C: Label Weights
    label_names = [l["name"].lower() for l in issue["labels"]["nodes"]]
    label_score = sum(LABEL_WEIGHTS.get(label, 0.0) for label in label_names)
    
    # Combined Formula
    final_score = (
        (unique_user_count * WEIGHT_UNIQUE_USERS) +
        (total_comments * WEIGHT_TOTAL_COMMENTS) +
        label_score
    )
    
    return round(final_score, 2)

# -------------------------------------------------------------------
# 4. Write Item & Score into your GitHub Projects v2 Board
# -------------------------------------------------------------------
def sync_to_github_project(project_id, field_id, issue_node_id, score):
    # Step A: Import External Issue Node into your Project Board
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
    print(f"Connecting to GitHub Projects (v2) for '{ORG_NAME}'...")
    project_id, field_id = get_project_and_field_ids()

    for target in TARGET_REPOS:
        print(f"\nProcessing External Repository: {target}")
        issues = fetch_external_repo_issues(target)
        
        for issue in issues:
            score = compute_priority_score(issue)
            
            sync_to_github_project(project_id, field_id, issue["id"], score)
            print(f"  └─ Issue #{issue['number']} ('{issue['title'][:30]}...') -> Priority Score: {score}")

if __name__ == "__main__":
    main()