import html
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
import textwrap
import unicodedata
if __package__:
    from .github_client import GitHubClient
else:
    from github_client import GitHubClient

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

client = GitHubClient(GH_TOKEN)
# Retrieve open issues updated within the past five years while retaining the
# 730-day activity requirement below.
ISSUE_LOOKBACK_DAYS = 1825
ISSUE_LOOKBACK_START = datetime.now(timezone.utc) - timedelta(days=ISSUE_LOOKBACK_DAYS)
ISSUE_LOOKBACK_TIMESTAMP = ISSUE_LOOKBACK_START.isoformat()

STANDARD_LOOKBACK_DAYS = 730
TWO_YEARS_AGO = datetime.now(timezone.utc) - timedelta(days=STANDARD_LOOKBACK_DAYS)
SINCE_TWO_YEARS_TIMESTAMP = TWO_YEARS_AGO.isoformat()

ISSUE_BATCH_SIZE = 10
SNAPSHOT_DIRECTORY = Path(__file__).resolve().parent.parent / "snapshot-history"

def run_graphql(query, variables=None):
    return client.request(
        "POST", GH_GRAPHQL_URL, graphql=True,
        mutation=query.lstrip().startswith("mutation"),
        json={"query": query, "variables": variables},
    )

# -------------------------------------------------------------------
# 1. Organization GitHub Project v2 Discovery & Snapshot
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

def get_project_items(project_id):
    query = """
    query($projectId: ID!, $after: String, $scoreName: String!, $sourceName: String!) {
      node(id: $projectId) {
        ... on ProjectV2 {
          items(first: 100, after: $after) {
            pageInfo { hasNextPage endCursor }
            nodes {
              id
              content {
                ... on Issue {
                  id
                  number
                  title
                  url
                  repository { nameWithOwner }
                }
              }
              score: fieldValueByName(name: $scoreName) {
                ... on ProjectV2ItemFieldNumberValue { number }
              }
              source: fieldValueByName(name: $sourceName) {
                ... on ProjectV2ItemFieldTextValue { text }
              }
            }
          }
        }
      }
    }
    """
    items = []
    after = None
    while True:
        data = run_graphql(query, {
            "projectId": project_id, "after": after,
            "scoreName": SCORE_FIELD_NAME, "sourceName": REPO_FIELD_NAME,
        })
        connection = data["node"]["items"]
        items.extend(connection["nodes"])
        if not connection["pageInfo"]["hasNextPage"]:
            break
        next_cursor = connection["pageInfo"]["endCursor"]
        if not next_cursor or next_cursor == after:
            raise RuntimeError("Project pagination did not advance.")
        after = next_cursor
    return items

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

def fetch_issue_graphql_details(node_ids):
    query = """
    query($ids: [ID!]!) {
      nodes(ids: $ids) {
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
    data = run_graphql(query, {"ids": node_ids})
    details = {node["id"]: node for node in data["nodes"] if node}
    if set(details) != set(node_ids):
        raise RuntimeError("GitHub returned incomplete issue details; preserving the Project.")
    return details

def fetch_external_repo_issues(repo_full_name):
    parts = repo_full_name.strip().split("/")
    if len(parts) != 2 or not all(parts):
        raise ValueError(f"Invalid target format '{repo_full_name}'. Expected 'owner/repo'.")

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
        response = client.request("GET", url, params=params.copy())

        raw_issues = response.json()
        candidates = []
        for issue in raw_issues:
            if "pull_request" in issue:
                continue
            if "comments" in issue and issue["comments"] < MIN_RECENT_COMMENTS:
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

            candidates.append((issue, label_names))

        for start in range(0, len(candidates), ISSUE_BATCH_SIZE):
            batch = candidates[start:start + ISSUE_BATCH_SIZE]
            details = fetch_issue_graphql_details([issue["node_id"] for issue, _ in batch])
            for issue, label_names in batch:
                gql_details = details[issue["node_id"]]
                comments = gql_details["comments"]["nodes"]

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
def sync_to_github_project(project_id, score_field_id, repo_field_id, issue_node_id, repo_full_name, score, existing_item=None):
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
    if existing_item is None:
        item_data = run_graphql(add_item_mutation, {"projectId": project_id, "contentId": issue_node_id})
        item_node = item_data["addProjectV2ItemById"]["item"]
        existing_item = {"id": item_node["id"]}
        if item_node.get("type") != "ISSUE" or "moby" in repo_full_name.lower():
            time.sleep(0.5)
    item_id = existing_item["id"]

    # 2. Update Priority Score (Number Field)
    update_fields_mutation = """
    mutation($projectId: ID!, $itemId: ID!, $scoreFieldId: ID!, $scoreValue: Float!,
             $repoFieldId: ID!, $textValue: String!, $updateScore: Boolean!, $updateSource: Boolean!) {
      score: updateProjectV2ItemFieldValue(
        input: {
          projectId: $projectId
          itemId: $itemId
          fieldId: $scoreFieldId
          value: { number: $scoreValue }
        }
      ) @include(if: $updateScore) {
        projectV2Item { id }
      }
      source: updateProjectV2ItemFieldValue(
        input: {
          projectId: $projectId
          itemId: $itemId
          fieldId: $repoFieldId
          value: { text: $textValue }
        }
      ) @include(if: $updateSource) {
        projectV2Item { id }
      }
    }
    """
    update_score = (existing_item.get("score") or {}).get("number") != float(score)
    update_source = (existing_item.get("source") or {}).get("text") != repo_full_name
    if update_score or update_source:
        run_graphql(update_fields_mutation, {
            "projectId": project_id, "itemId": item_id,
            "scoreFieldId": score_field_id, "scoreValue": float(score),
            "repoFieldId": repo_field_id, "textValue": repo_full_name,
            "updateScore": update_score, "updateSource": update_source,
        })


def reconcile_project(project_id, score_field_id, repo_field_id, desired_issues):
    items = get_project_items(project_id)
    existing = {
        item["content"]["id"]: item
        for item in items if item.get("content") and item["content"].get("id")
    }
    desired_ids = {issue["id"] for issue, _ in desired_issues}
    for issue, score in desired_issues:
        sync_to_github_project(
            project_id, score_field_id, repo_field_id,
            issue["id"], issue["repo_full_name"], score,
            existing_item=existing.get(issue["id"]),
        )
        print(f"  └─ [SYNCED] Issue #{issue['number']} ({issue['repo_full_name']}) -> Priority Score: {score}")

    # Remove stale monitored issues only after every desired item is synced.
    targets = {repo.lower() for repo in TARGET_REPOS}
    delete_mutation = """
    mutation($projectId: ID!, $itemId: ID!) {
      deleteProjectV2Item(input: {projectId: $projectId, itemId: $itemId}) {
        deletedItemId
      }
    }
    """
    for item in items:
        content = item.get("content") or {}
        repo_name = (content.get("repository") or {}).get("nameWithOwner", "")
        source = (item.get("source") or {}).get("text", "")
        if (
            repo_name.lower() in targets
            and source.lower() == repo_name.lower()
            and content.get("id") not in desired_ids
        ):
            run_graphql(delete_mutation, {"projectId": project_id, "itemId": item["id"]})


def _pdf_text(value):
    normalized = unicodedata.normalize("NFKD", str(value))
    return normalized.encode("ascii", "replace").decode("ascii")


def _escape_pdf_text(value):
    return _pdf_text(value).replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _write_table_pdf(rows, output_path, column_widths, page_size, margin,
                     header_row_index=0):
    if not rows or not column_widths:
        raise ValueError("PDF tables require rows and column widths")
    if not 0 <= header_row_index < len(rows):
        raise ValueError("PDF table header row is out of range")
    if sum(column_widths) + 2 * margin > page_size[0]:
        raise ValueError("PDF table columns exceed the page width")

    column_count = len(column_widths)
    normalized_rows = []
    for row in rows:
        if len(row) != column_count:
            raise ValueError("PDF table rows must match the column count")
        normalized_rows.append([
            textwrap.wrap(
                _pdf_text(value),
                width=max(1, int((column_width - 12) / 4.5)),
                break_long_words=True,
                break_on_hyphens=True,
            ) or [""]
            for value, column_width in zip(row, column_widths)
        ])

    page_width, page_height = page_size
    bottom = margin
    x_start = margin
    table_width = sum(column_widths)
    streams = []
    commands = []
    cursor_y = page_height - margin

    def draw_row(cells, header=False):
        nonlocal cursor_y
        row_height = max(map(len, cells)) * 10 + 8
        if cursor_y - row_height < bottom:
            return False
        y_bottom = cursor_y - row_height
        if header:
            commands.append(f"0.88 g {x_start} {y_bottom} {table_width} {row_height} re f")
        commands.append("0 g")
        commands.append(f"0.45 G 0.6 w {x_start} {y_bottom} {table_width} {row_height} re S")
        x = x_start
        for cell_lines, column_width in zip(cells, column_widths):
            commands.append(f"{x} {y_bottom} {column_width} {row_height} re S")
            for line_index, line in enumerate(cell_lines):
                baseline = cursor_y - 13 - line_index * 10
                commands.append(
                    f"BT /F1 8 Tf {x + 6} {baseline} Td "
                    f"({_escape_pdf_text(line)}) Tj ET"
                )
            x += column_width
        cursor_y = y_bottom
        return True

    def new_page():
        nonlocal commands, cursor_y
        if commands:
            streams.append("\n".join(commands).encode("ascii"))
        commands = ["0 G"]
        cursor_y = page_height - margin

    header = normalized_rows[header_row_index]
    for row_index, row in enumerate(normalized_rows):
        if not draw_row(row, header=row_index == header_row_index):
            new_page()
            if row_index > header_row_index:
                draw_row(header, header=True)
            if not draw_row(row):
                raise ValueError("PDF table row exceeds the page height")
    if commands:
        streams.append("\n".join(commands).encode("ascii"))

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    page_refs = []
    for stream in streams:
        page_number = len(objects) + 1
        content_number = page_number + 1
        page_refs.append(f"{page_number} 0 R")
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {page_width} {page_height}] "
                "/Resources << /Font << /F1 3 0 R >> >> /Contents "
                f"{content_number} 0 R >>"
            ).encode("ascii")
        )
        objects.append(
            b"<< /Length "
            + str(len(stream)).encode("ascii")
            + b" >>\nstream\n"
            + stream
            + b"\nendstream"
        )
    objects[1] = (
        b"<< /Type /Pages /Kids ["
        + " ".join(page_refs).encode("ascii")
        + b"] /Count "
        + str(len(page_refs)).encode("ascii")
        + b" >>"
    )

    pdf = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for object_number, content in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf.extend(f"{object_number} 0 obj\n".encode("ascii"))
        pdf.extend(content)
        pdf.extend(b"\nendobj\n")
    xref_offset = len(pdf)
    pdf.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    pdf.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        pdf.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    pdf.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode("ascii")
    )
    output_path.write_bytes(pdf)


def _write_document_pdf(sections, output_path, page_size=(842, 595), margin=36):
    page_width, page_height = page_size
    bottom = margin
    streams = []
    commands = ["0 G"]
    cursor_y = page_height - margin

    def save_page():
        nonlocal commands, cursor_y
        if commands:
            streams.append("\n".join(commands).encode("ascii"))
        commands = ["0 G"]
        cursor_y = page_height - margin

    def text_lines(value, width, font_size=8):
        width_chars = max(1, int((width - 12) / (font_size * 0.55)))
        return textwrap.wrap(
            _pdf_text(value), width=width_chars,
            break_long_words=True, break_on_hyphens=True,
        ) or [""]

    def draw_bullets(items):
        nonlocal cursor_y
        for item in items:
            wrapped = text_lines(item, page_width - 2 * margin - 16, 9)
            row_height = max(14, len(wrapped) * 12 + 4)
            if cursor_y - row_height < bottom:
                save_page()
            baseline = cursor_y - 13
            commands.append(
                f"{margin + 3} {baseline - 1} m {margin + 3} {baseline + 3} "
                f"{margin + 7} {baseline + 3} {margin + 7} {baseline - 1} c "
                f"{margin + 7} {baseline - 5} {margin + 3} {baseline - 5} "
                f"{margin + 3} {baseline - 1} c f"
            )
            for line_index, line in enumerate(wrapped):
                commands.append(
                    f"BT /F1 9 Tf {margin + 14} {baseline - line_index * 12} Td "
                    f"({_escape_pdf_text(line)}) Tj ET"
                )
            cursor_y -= row_height

    def draw_table(title, rows, widths):
        nonlocal cursor_y
        if not rows or not widths or sum(widths) + 2 * margin > page_width:
            raise ValueError("Invalid PDF document table layout")
        if cursor_y - 24 < bottom:
            save_page()
        commands.append(
            f"BT /F1 11 Tf {margin} {cursor_y - 13} Td "
            f"({_escape_pdf_text(title)}) Tj ET"
        )
        cursor_y -= 22
        normalized = []
        for row in rows:
            if len(row) != len(widths):
                raise ValueError("PDF document table rows must match the column count")
            normalized.append([
                text_lines(value, width)
                for value, width in zip(row, widths)
            ])
        table_width = sum(widths)
        header = normalized[0]

        def draw_row(cells, is_header=False):
            nonlocal cursor_y
            row_height = max(map(len, cells)) * 10 + 8
            if cursor_y - row_height < bottom:
                return False
            y_bottom = cursor_y - row_height
            if is_header:
                commands.append(f"0.88 g {margin} {y_bottom} {table_width} {row_height} re f")
            commands.append("0 g")
            commands.append(
                f"0.45 G 0.6 w {margin} {y_bottom} {table_width} {row_height} re S"
            )
            x = margin
            for cell_lines, width in zip(cells, widths):
                commands.append(f"{x} {y_bottom} {width} {row_height} re S")
                for line_index, line in enumerate(cell_lines):
                    commands.append(
                        f"BT /F1 8 Tf {x + 6} {cursor_y - 13 - line_index * 10} Td "
                        f"({_escape_pdf_text(line)}) Tj ET"
                    )
                x += width
            cursor_y = y_bottom
            return True

        for row_index, row in enumerate(normalized):
            if not draw_row(row, is_header=row_index == 0):
                save_page()
                if row_index:
                    draw_row(header, is_header=True)
                if not draw_row(row):
                    raise ValueError("PDF document table row exceeds the page height")
        cursor_y -= 14

    for section in sections:
        if section["type"] == "bullets":
            draw_bullets(section["items"])
        elif section["type"] == "table":
            draw_table(section["title"], section["rows"], section["widths"])
        else:
            raise ValueError(f"Unknown PDF document section: {section['type']}")
    if commands:
        streams.append("\n".join(commands).encode("ascii"))

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    page_refs = []
    for stream in streams:
        page_number = len(objects) + 1
        content_number = page_number + 1
        page_refs.append(f"{page_number} 0 R")
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {page_width} {page_height}] "
                "/Resources << /Font << /F1 3 0 R >> >> /Contents "
                f"{content_number} 0 R >>"
            ).encode("ascii")
        )
        objects.append(
            b"<< /Length "
            + str(len(stream)).encode("ascii")
            + b" >>\nstream\n"
            + stream
            + b"\nendstream"
        )
    objects[1] = (
        b"<< /Type /Pages /Kids ["
        + " ".join(page_refs).encode("ascii")
        + f"] /Count {len(page_refs)} >>".encode("ascii")
    )
    pdf = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for object_number, content in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf.extend(f"{object_number} 0 obj\n".encode("ascii"))
        pdf.extend(content)
        pdf.extend(b"\nendobj\n")
    xref_offset = len(pdf)
    pdf.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    pdf.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        pdf.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    pdf.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode("ascii")
    )
    output_path.write_bytes(pdf)


def _write_pdf(lines, output_path, table_rows=None, column_widths=None,
               page_size=(612, 842), margin=40, header_row_index=0,
               document_sections=None):
    if document_sections is not None:
        _write_document_pdf(document_sections, output_path)
        return
    if table_rows is not None:
        _write_table_pdf(
            table_rows, output_path, column_widths, page_size, margin,
            header_row_index,
        )
        return

    wrapped_lines = []
    for line in lines:
        wrapped_lines.extend(textwrap.wrap(_pdf_text(line), width=94) or [""])

    lines_per_page = 48
    page_lines = [
        wrapped_lines[index:index + lines_per_page]
        for index in range(0, len(wrapped_lines), lines_per_page)
    ] or [[]]

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"",  # Filled with the page tree after page objects are created.
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    page_refs = []
    for page in page_lines:
        page_number = len(objects) + 1
        content_number = page_number + 1
        page_refs.append(f"{page_number} 0 R")
        commands = ["BT", "/F1 9 Tf", "50 790 Td", "12 TL"]
        for line in page:
            commands.append(f"({_escape_pdf_text(line)}) Tj")
            commands.append("T*")
        commands.append("ET")
        stream = "\n".join(commands).encode("ascii")
        objects.append(
            (
                b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 842] "
                b"/Resources << /Font << /F1 3 0 R >> >> /Contents "
                + f"{content_number} 0 R >>".encode("ascii")
            )
        )
        objects.append(
            b"<< /Length "
            + str(len(stream)).encode("ascii")
            + b" >>\nstream\n"
            + stream
            + b"\nendstream"
        )
    objects[1] = (
        b"<< /Type /Pages /Kids ["
        + " ".join(page_refs).encode("ascii")
        + b"] /Count "
        + str(len(page_refs)).encode("ascii")
        + b" >>"
    )

    pdf = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for object_number, content in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf.extend(f"{object_number} 0 obj\n".encode("ascii"))
        pdf.extend(content)
        pdf.extend(b"\nendobj\n")

    xref_offset = len(pdf)
    pdf.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    pdf.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        pdf.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    pdf.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode("ascii")
    )
    output_path.write_bytes(pdf)


def write_project_snapshot(project_id, items=None, output_directory=None, generated_at=None):
    if items is None:
        items = get_project_items(project_id)
    generated_at = generated_at or datetime.now(timezone.utc)
    project_url = f"https://github.com/orgs/{ORGANIZATION_NAME}/projects/{PROJECT_NUMBER}"
    lines = [
        "Windows Container Issue Snapshot",
        f"Generated: {generated_at:%Y-%m-%d %H:%M:%S UTC}",
        f"ProjectV2: {project_url}",
        f"Items: {len(items)}",
        "",
    ]
    table_rows = [
        ("Windows Container Issue Snapshot", "", "", "", ""),
        ("Generated", "", f"{generated_at:%Y-%m-%d %H:%M:%S UTC}", "", ""),
        ("ProjectV2", "", project_url, "", ""),
        ("Items", "", str(len(items)), "", ""),
        ("Repository", "Issue", "Title", "Priority score", "URL"),
    ]
    for item in items:
        content = item.get("content") or {}
        repository = (content.get("repository") or {}).get("nameWithOwner", "Unknown repository")
        number = content.get("number")
        title = content.get("title") or "Untitled issue"
        issue_ref = f"#{number}" if number is not None else "Draft item"
        score = (item.get("score") or {}).get("number")
        table_rows.append((
            repository,
            issue_ref,
            title,
            str(score) if score is not None else "Not set",
            content.get("url", ""),
        ))

    output_directory = Path(output_directory or SNAPSHOT_DIRECTORY)
    output_directory.mkdir(parents=True, exist_ok=True)
    output_path = output_directory / (
        f"windows-container-issue-snapshot-{generated_at:%Y-%m-%d}.pdf"
    )
    _write_pdf(
        lines, output_path, table_rows=table_rows,
        column_widths=(110, 55, 260, 85, 260), page_size=(842, 595), margin=36,
        header_row_index=4,
    )
    print(f"Project snapshot generated: {output_path}")
    return output_path


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

def build_digest_body(top_issues_by_repo, digest_issue_url=""):
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

    digest_issue_link = ""
    if digest_issue_url:
        safe_digest_issue_url = html.escape(digest_issue_url, quote=True)
        digest_issue_link = (
            f'<p><a href="{safe_digest_issue_url}">'
            "View the durable digest issue"
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
{digest_issue_link}
""".strip()

def find_existing_digest_issue(repo_full_name, run_id):
    owner, repo = repo_full_name.split("/", 1)
    params = {
        "state": "all",
        "per_page": 100
    }
    if DIGEST_LABELS:
        params["labels"] = DIGEST_LABELS[0]

    response = client.request(
        "GET",
        f"{GH_REST_URL}/repos/{owner}/{repo}/issues",
        params=params,
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

    response = client.request(
        "POST",
        f"{GH_REST_URL}/repos/{owner}/{repo}/issues",
        mutation=True,
        json={
            "title": title,
            "body": build_digest_body(top_issues_by_repo),
            "labels": DIGEST_LABELS
        },
    )
    response.raise_for_status()

    digest_issue = response.json()
    print(f"Weekly digest published: {digest_issue['html_url']}")
    return digest_issue

# -------------------------------------------------------------------
# Execution Entry Point
# -------------------------------------------------------------------
def main():
    if not TARGET_REPOS:
        raise ValueError("TARGET_REPOS must contain at least one repository.")
    print(f"Connecting to GitHub Projects (v2) for organization '{ORGANIZATION_NAME}'...")
    print(f"Microsoft/Azure issue lookback starts: {ISSUE_LOOKBACK_TIMESTAMP[:10]}")
    print(f"Other repository issue lookback starts: {SINCE_TWO_YEARS_TIMESTAMP[:10]}")
    print(f"Filtering issues with >= {MIN_RECENT_COMMENTS} comments since: {SINCE_TWO_YEARS_TIMESTAMP[:10]}")
    top_issues_by_repo = {}
    project_id, score_field_id, repo_field_id = get_project_and_field_ids()

    # Collect all repositories before changing any Project items.
    desired_issues = []
    all_scored_issues = []
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
        all_scored_issues.extend(scored_issues)
        top_issues_by_repo[target] = scored_issues[:3]

        desired_issues.extend(scored_issues[:MAX_PROJECT_ISSUES_PER_REPO])

    reconcile_project(project_id, score_field_id, repo_field_id, desired_issues)
    publish_digest = (
        os.getenv("PUBLISH_WEEKLY_DIGEST", "").strip().lower() == "true"
    )
    digest_issue = None
    if publish_digest:
        digest_issue = publish_weekly_digest(top_issues_by_repo)
    write_project_snapshot(project_id)
    write_scored_issues(all_scored_issues)


def write_scored_issues(scored_issues, output_path=None):
    output_path = Path(output_path or os.getenv(
        "SCORED_ISSUES_PATH",
        str(Path(__file__).resolve().parent.parent / "scoring-output" / "scored-issues.json"),
    ))
    if not output_path.is_absolute():
        output_path = Path(__file__).resolve().parent.parent / output_path
    records = [
        {**{key: issue[key] for key in ("id", "number", "title", "url", "repo_full_name")},
         "score": score}
        for issue, score in scored_issues
    ]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    return output_path

if __name__ == "__main__":
    main()