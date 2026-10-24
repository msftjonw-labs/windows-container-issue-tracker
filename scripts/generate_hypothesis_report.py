"""Conservative, deterministic evidence triage of all exported scored issues."""

import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
import re
import sys

if __package__:
    from .calculate_scores import _write_pdf, run_graphql
    from .hypothesis_evolution import evolve, validate_state
else:
    from calculate_scores import _write_pdf, run_graphql
    from hypothesis_evolution import evolve, validate_state


ROOT = Path(__file__).resolve().parent.parent
HYPOTHESES = (
    "The community does not want to use Windows containers as the development platform because it is not free and lacks open source community popularity.",
    "The Windows containers community has not grown in the past few years because gaps in the Nano Server container image limit what developers can test on top of it.",
)
DETAIL_FIELDS = """
  __typename id number title body state updatedAt url
  labels(first: 100) { nodes { name } }
  comments(last: 100) {
    totalCount
    nodes { body url createdAt updatedAt }
  }
"""
ISSUE_QUERY = """
query HypothesisIssue($id: ID!) {
  node(id: $id) {
    ... on Issue {
""" + DETAIL_FIELDS + """
      repository { nameWithOwner isPrivate }
    }
  }
}
"""
REFERENCES_QUERY = """
query HypothesisReferences($id: ID!, $after: String) {
  node(id: $id) {
    ... on Issue {
      timelineItems(first: 100, after: $after,
                    itemTypes: [CROSS_REFERENCED_EVENT, CONNECTED_EVENT]) {
        pageInfo { hasNextPage endCursor }
        nodes {
          __typename
          ... on CrossReferencedEvent {
            source { __typename ... on PullRequest { id updatedAt } }
          }
          ... on ConnectedEvent {
            subject { __typename ... on PullRequest { id updatedAt } }
          }
        }
      }
    }
  }
}
"""
PR_QUERY = """
query HypothesisPullRequest($id: ID!) {
  node(id: $id) {
    ... on PullRequest {
""" + DETAIL_FIELDS + """
      mergedAt
      repository { nameWithOwner isPrivate }
    }
  }
}
"""


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("Missing or invalid GitHub timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("GitHub timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def input_path():
    path = Path(os.environ.get("SCORED_ISSUES_PATH", "scoring-output/scored-issues.json"))
    return path if path.is_absolute() else ROOT / path


def load_scored_issues(path):
    """The export, not the Project's top-ten subset, defines the entire sample."""
    issues = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(issues, list):
        raise ValueError("Scored issue export must be a JSON list")
    seen = set()
    for issue in issues:
        if not isinstance(issue, dict):
            raise ValueError("Invalid scored issue")
        for field in ("id", "title", "url", "repo_full_name"):
            if not isinstance(issue.get(field), str) or not issue[field].strip():
                raise ValueError(f"Missing scored issue {field}")
        if type(issue.get("number")) is not int or issue["number"] < 1:
            raise ValueError("Invalid scored issue number")
        score = issue.get("score")
        if type(score) not in (int, float) or not math.isfinite(score):
            raise ValueError("Invalid scored issue score")
        if issue["id"] in seen:
            raise ValueError("Duplicate scored issue id")
        seen.add(issue["id"])
    return issues


def query_node(query, variables):
    # run_graphql uses the shared serial, rate-aware GitHubClient.
    data = run_graphql(query, variables)
    if not isinstance(data, dict) or data.get("errors"):
        raise ValueError("GitHub returned an invalid or partial response")
    node = data.get("node")
    if not isinstance(node, dict) or not node:
        raise ValueError("GitHub node is missing or inaccessible")
    return node


def validate_details(node, kind, expected_id):
    if node.get("__typename") != kind or node.get("id") != expected_id:
        raise ValueError(f"Expected accessible {kind} {expected_id}")
    for key in ("title", "body", "state", "updatedAt", "url"):
        if not isinstance(node.get(key), str):
            raise ValueError(f"Missing {kind} {key}")
    if not node["url"] or not node["title"]:
        raise ValueError(f"Missing {kind} title or URL")
    if type(node.get("number")) is not int:
        raise ValueError(f"Missing {kind} number")
    if node["state"] not in (("OPEN", "CLOSED") if kind == "Issue" else ("OPEN", "CLOSED", "MERGED")):
        raise ValueError(f"Invalid {kind} state")
    timestamp(node["updatedAt"])
    repository = node.get("repository")
    if (not isinstance(repository, dict)
            or not isinstance(repository.get("nameWithOwner"), str)
            or not re.fullmatch(r"[\w.-]+/[\w.-]+", repository["nameWithOwner"])
            or type(repository.get("isPrivate")) is not bool):
        raise ValueError(f"Missing or invalid {kind} repository visibility")
    labels = node.get("labels")
    if (not isinstance(labels, dict) or not isinstance(labels.get("nodes"), list)
            or any(not isinstance(label, dict) or not isinstance(label.get("name"), str)
                   for label in labels["nodes"])):
        raise ValueError(f"Missing or invalid {kind} labels")
    comments = node.get("comments")
    if not isinstance(comments, dict) or not isinstance(comments.get("nodes"), list):
        raise ValueError(f"Missing {kind} comments")
    count = comments.get("totalCount")
    if type(count) is not int or count < 0 or len(comments["nodes"]) != min(count, 100):
        raise ValueError(f"Incomplete {kind} latest comments")
    for comment in comments["nodes"]:
        if not isinstance(comment, dict):
            raise ValueError("Missing comment")
        for key in ("body", "url", "createdAt", "updatedAt"):
            if not isinstance(comment.get(key), str):
                raise ValueError(f"Missing comment {key}")
        if not comment["url"]:
            raise ValueError("Missing comment URL")
        timestamp(comment["createdAt"])
        timestamp(comment["updatedAt"])
    if kind == "PullRequest":
        if "mergedAt" not in node:
            raise ValueError("Missing PR merge status")
        if node["mergedAt"] is not None:
            timestamp(node["mergedAt"])
        if (node["state"] == "MERGED") != (node["mergedAt"] is not None):
            raise ValueError("Inconsistent PR merge status")
    return node


def latest_linked_pr(issue_id):
    """Collect genuine PR nodes from every page; never parse links in prose."""
    after = None
    cursors = set()
    references = {}
    while True:
        node = query_node(REFERENCES_QUERY, {"id": issue_id, "after": after})
        connection = node.get("timelineItems")
        if not isinstance(connection, dict) or not isinstance(connection.get("nodes"), list):
            raise ValueError("Missing issue timeline references")
        for event in connection["nodes"]:
            if not isinstance(event, dict):
                raise ValueError("Missing timeline event")
            event_type = event.get("__typename")
            if event_type not in ("CrossReferencedEvent", "ConnectedEvent"):
                raise ValueError("Unexpected timeline event type")
            reference = event.get("source" if event_type == "CrossReferencedEvent" else "subject")
            if not isinstance(reference, dict) or not reference.get("__typename"):
                raise ValueError("Missing timeline reference (possibly inaccessible)")
            if reference["__typename"] != "PullRequest":
                continue
            if not isinstance(reference.get("id"), str) or not reference["id"]:
                raise ValueError("Missing linked PR id")
            updated = timestamp(reference.get("updatedAt"))
            references[reference["id"]] = max(updated, references.get(reference["id"], updated))
        info = connection.get("pageInfo")
        if not isinstance(info, dict) or type(info.get("hasNextPage")) is not bool:
            raise ValueError("Missing timeline pagination information")
        if not info["hasNextPage"]:
            break
        after = info.get("endCursor")
        if not isinstance(after, str) or not after or after in cursors:
            raise ValueError("Invalid or repeated timeline pagination cursor")
        cursors.add(after)
    if not references:
        return None, 0
    latest_id = max(references, key=lambda ref: (references[ref], ref))
    pr = validate_details(query_node(PR_QUERY, {"id": latest_id}), "PullRequest", latest_id)
    return pr, len(references)


def collect_issue(scored):
    issue = validate_details(
        query_node(ISSUE_QUERY, {"id": scored["id"]}), "Issue", scored["id"]
    )
    repository = (issue.get("repository") or {}).get("nameWithOwner")
    if not isinstance(repository, str) or repository.casefold() != scored["repo_full_name"].casefold() or issue["number"] != scored["number"]:
        raise ValueError("Scored issue identity does not match fresh GitHub issue")
    pr, reference_count = latest_linked_pr(scored["id"])
    return {"scored": scored, "issue": issue, "pr": pr, "reference_count": reference_count}


def documents(record):
    for label, node in (("Issue", record["issue"]), ("Latest linked PR", record["pr"])):
        if node is None:
            continue
        yield f"{label} title/body", node["title"] + "\n\n" + node["body"], node["url"], node["updatedAt"]
        for comment in node["comments"]["nodes"]:
            yield f"{label} comment", comment["body"], comment["url"], comment["updatedAt"]


def excerpt(text, limit=1800):
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + " [excerpt truncated; review full source]"


def triage(record, hypothesis):
    """Lexical candidates only: even a supporting candidate is not validation."""
    signals = []
    for label, text, url, date in documents(record):
        # A paragraph retains negation and nearby context; no cross-document joins.
        for paragraph in re.split(r"\n\s*\n", text):
            value = paragraph.lower()
            windows = bool(re.search(r"\bwindows[\s-]+containers?\b", value))
            nano = bool(re.search(r"\bnano[\s-]*server\b", value))
            cost = bool(re.search(r"\b(cost|costs|licen[cs]\w*|paid|free)\b", value))
            popularity = bool(re.search(r"\b(popular\w*|open[\s-]source|community|adoption)\b", value))
            gap = bool(re.search(r"\b(missing|lack\w*|unsupported|unavailable|gap\w*|limit\w*)\b", value))
            testing = bool(re.search(r"\b(test\w*|develop\w*)\b", value))
            if not (windows and (cost or popularity) if hypothesis == 1 else nano and (gap or testing)):
                continue
            # "Not free" asserts a cost concern; retain all other negation checks.
            negation_context = re.sub(r"\bnot\s+free\b", "paid", value) if hypothesis == 1 else value
            negated = bool(re.search(r"\b(no|not|never|without|isn't|aren't|doesn't|don't|cannot|can't)\b", negation_context))
            category = "inconclusive"
            reason = "Topic keywords only; intent, scope and causal meaning need human review."
            if hypothesis == 1:
                counter = re.search(r"\b(cost is not a barrier|licensing is not a barrier|we use windows containers for development)\b", value)
                barrier = re.search(r"\b(prevents? adoption|avoid\w*|barrier\w*|too expensive|unpopular)\b", value)
                if counter:
                    category = "contradicting"
                    reason = "Explicit local use/non-barrier wording; not a population-level counterexample."
                elif cost and popularity and barrier and testing and not negated:
                    category = "supporting"
                    reason = "Co-occurring development, cost, community and barrier terms; not proof of the compound hypothesis."
            elif nano and re.search(r"\b(now supports|gap is resolved|tests pass|no longer limits)\b", value):
                category = "contradicting"
                reason = "Possible local gap resolution/test success; does not measure community growth."
            elif gap and testing and not negated:
                category = "supporting"
                reason = "Possible Nano Server testing limitation only; no evidence of growth or its cause."
            if negated and category == "inconclusive":
                reason = "Negation/qualification present; keyword matches must not be treated as support."
            signals.append({
                "category": category, "reason": reason, "excerpt": excerpt(paragraph),
                "source": label, "url": url, "date": date,
            })
    return signals


def build_report(records, generated_at, source_path, adaptive_lines=()):
    if not adaptive_lines:
        _, adaptive_lines = evolve(records, generated_at, None, HYPOTHESES, triage)
    lines = [
        "Windows Container Hypothesis Validation - Evidence Triage",
        f"Generated: {generated_at:%Y-%m-%d %H:%M:%S UTC}",
        f"Scored issue export: {source_path}",
        f"Total analyzed issues: {len(records)} (all exported qualifying scored issues; no research cap)",
        "",
    ]
    lines.extend(adaptive_lines)
    lines.extend([
        "",
        "Original seed hypotheses (historical reference only)",
        "Seed 1: " + HYPOTHESES[0],
        "Seed 2: " + HYPOTHESES[1],
        "",
        "Method and limitations",
        "Fresh issue title/body/state/updatedAt/URL, first 100 labels and latest 100 comments; all pages of",
        "cross-referenced and connected timeline events are inspected for actual PR nodes.",
        "Only the most recently updated linked PR is examined (ties: node id), including",
        "its current title/body/state/mergedAt and latest 100 comments. References are not endorsements.",
        "Requests are serial via the shared rate-aware GitHub client. No external LLM or",
        "arbitrary URLs from issue text are fetched. API/missing-data errors abort the report.",
        "Deterministic paragraph-level keyword rules produce supporting, contradicting and",
        "inconclusive candidate signals, not automated semantic truth or hypothesis proof.",
        "Original seed 1 lexical support requires Windows containers, cost, community, development and",
        "barrier terms together without detected negation, except affirmative 'not free'",
        "cost wording. Other negation, including 'not a barrier' or 'do not avoid', remains",
        "disqualifying for seed 1 support. Original seed 2 lexical support requires",
        "Nano Server gap/testing terms without detected negation. Explicit non-barrier/use",
        "or resolved-gap phrases can be contradicting candidates; other matches are inconclusive.",
        "Rules can miss paraphrases, sarcasm, code, quotations, historical context and negation;",
        "unrelated Windows needs and topic mentions are not definitive support.",
        "Triage retains paragraph context up to 1800 characters; showcase aggregates supporting",
        "signal keys/excerpts by source URL into at most 500 characters, with truncation marked.",
        "Source dates are last-updated timestamps, not necessarily when a claim was made.",
        "Comments older than the latest 100 and non-selected PR contents are outside scope.",
        "A scored issue sample cannot establish prevalence, causality, community growth,",
        "or represent nonadopters. Activity, scores, open/closed/merged states do not",
        "establish popularity or growth. This report does not determine licensing/free-use truth.",
    ])
    lines.extend([
        "Research limitations",
        "Do not interpret candidate counts as prevalence or validation. Review full sources",
        "and obtain representative adopter/nonadopter research and longitudinal growth data.",
    ])
    return lines


def _report_table_rows(lines):
    headings = (
        "Adaptive weekly hypotheses",
        "Original seed hypotheses",
        "Method and limitations",
        "Research limitations",
        "Researched areas (",
        "Adaptive method and limitations",
        "Current supporting source showcase",
    )
    rows = [("Section", "Report content")]
    section = "Report overview"
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        if index == 0:
            rows.append((section, line))
            continue
        if line.startswith(headings):
            section = line
            rows.append((section, ""))
        else:
            rows.append((section, line))
    return rows


def _report_pdf_sections(lines):
    summary = [lines[0], lines[1]]
    summary.extend(line for line in lines if line.startswith("Total analyzed issues:"))
    hypotheses = {}
    for index, line in enumerate(lines):
        match = re.match(r"^(H[12]) \[([^]]+)\]\s*(.*)$", line)
        if not match:
            continue
        hypothesis_id, key, title = match.groups()
        decision = lines[index + 1] if index + 1 < len(lines) else "Decision: unavailable"
        hypotheses[hypothesis_id] = {
            "key": key,
            "title": title or key,
            "decision": decision.removeprefix("Decision: "),
            "title_index": index,
        }
    observation = next(
        (line for line in lines if line.startswith("Observation week:")), None
    )
    if observation:
        summary.append(observation)
    omitted = {0, 1}
    omitted.update(
        index for index, line in enumerate(lines)
        if line.startswith("Total analyzed issues:")
    )
    evidence = {}
    for hypothesis_id in ("H1", "H2"):
        hypothesis = hypotheses.get(hypothesis_id)
        if hypothesis:
            summary.append(
                f"Hypothesis {hypothesis_id[-1]}: {hypothesis['title']}; "
                f"Decision: {hypothesis['decision']}"
            )
        evidence[hypothesis_id] = []

    sources = []
    for index, line in enumerate(lines):
        if not line.startswith("Current source |"):
            continue
        parts = [part.strip() for part in line.split("|")]
        if len(parts) < 4:
            continue
        source = {
            "type": parts[1],
            "repository": parts[2].removeprefix("repository: "),
            "updated": parts[3].removeprefix("updated: "),
            "title": "",
            "url": "",
            "keys": [],
            "excerpt": "",
        }
        for offset in range(1, 6):
            if index + offset >= len(lines):
                break
            detail = lines[index + offset]
            if detail.startswith("Source title: "):
                source["title"] = detail.removeprefix("Source title: ")
            elif detail.startswith("Supporting signal keys: "):
                source["keys"] = [
                    key.strip()
                    for key in detail.removeprefix("Supporting signal keys: ").split(",")
                    if key.strip()
                ]
            elif detail.startswith("Context excerpt: "):
                source["excerpt"] = detail.removeprefix("Context excerpt: ")
            elif detail.startswith("http"):
                source["url"] = detail
        sources.append(source)
        omitted.update(range(index, min(index + 6, len(lines))))

    for source in sources:
        for hypothesis_id, hypothesis in hypotheses.items():
            if hypothesis["key"] in source["keys"]:
                evidence[hypothesis_id].append([
                    source["repository"],
                    f"{source['type']} | {source['updated']}",
                    f"{source['title']}\nSupporting context: {source['excerpt']}",
                    source["url"],
                    ", ".join(source["keys"]),
                ])

    sections = [{"type": "bullets", "items": summary}]
    evidence_widths = (105, 100, 275, 190, 100)
    for hypothesis_id in ("H1", "H2"):
        hypothesis = hypotheses.get(hypothesis_id)
        if not hypothesis:
            continue
        rows = [[
            "Repository", "Source / updated", "Title and supporting context",
            "URL", "Signal keys",
        ]]
        rows.extend(evidence[hypothesis_id])
        if not evidence[hypothesis_id]:
            rows.append([
                "No current supporting sources selected", "", "", "", "",
            ])
        sections.append({
            "type": "table",
            "title": f"Hypothesis {hypothesis_id[-1]} Supporting evidence",
            "rows": rows,
            "widths": evidence_widths,
        })

    remaining = [line for index, line in enumerate(lines) if index not in omitted]
    detail_sections = (
        ("Adaptive weekly hypotheses (authoritative latest decisions)",
         "Adaptive weekly hypotheses (authoritative latest decisions)"),
        ("Researched areas (current evidence, not validated scientific findings)",
         "Researched areas"),
        ("Adaptive method and limitations", "Adaptive method and limitations"),
        ("Current supporting source showcase (separate from frozen historical decision references)",
         "Current supporting source showcase"),
        ("Original seed hypotheses (historical reference only)",
         "Original seed hypotheses (historical reference only)"),
        ("Method and limitations", "Method and limitations"),
        ("Research limitations", "Research limitations"),
    )
    sections.append({"type": "heading", "title": "Additional report details"})
    detail_groups = []
    current_title = "Other report details"
    current_items = []
    current_started = False
    for line in remaining:
        if not line.strip():
            continue
        heading = next(
            ((section_title, source_heading)
             for section_title, source_heading in detail_sections
             if line.startswith(source_heading)),
            None,
        )
        if heading:
            if current_items or current_started:
                detail_groups.append((current_title, current_items))
            current_title = heading[0]
            current_items = []
            current_started = True
        else:
            hypothesis_title = re.match(r"^H([12]) (?=\[)", line)
            current_items.append(
                f"Hypothesis {hypothesis_title[1]} {line[hypothesis_title.end():]}"
                if hypothesis_title else line
            )
    if current_items or current_started:
        detail_groups.append((current_title, current_items))
    for title, items in detail_groups:
        sections.extend([
            {"type": "heading", "title": title},
            {"type": "bullets", "items": items},
        ])
    return sections


def generate_report(scored_path=None, output_directory=None, generated_at=None,
                    previous_state=None, on_success=None):
    generated_at = generated_at or datetime.now(timezone.utc)
    if generated_at.tzinfo is None:
        raise ValueError("Report time must be timezone-aware")
    generated_at = generated_at.astimezone(timezone.utc)
    if previous_state is not None:
        validate_state(previous_state)
    source = Path(scored_path) if scored_path is not None else input_path()
    records = [collect_issue(issue) for issue in load_scored_issues(source)]
    new_state, adaptive_lines = evolve(records, generated_at, previous_state, HYPOTHESES, triage)
    lines = build_report(records, generated_at, source, adaptive_lines)
    directory = Path(output_directory) if output_directory is not None else ROOT / "report-history"
    directory.mkdir(parents=True, exist_ok=True)
    output = directory / f"windows-container-hypothesis-validation-{generated_at:%Y-%m-%d}.pdf"
    _write_pdf(
        lines, output, document_sections=_report_pdf_sections(lines),
    )
    if on_success is not None:
        on_success(new_state)
    return output


def main():
    try:
        output = generate_report()
    except Exception as error:
        print(f"Hypothesis report failed: {error}", file=sys.stderr)
        return 1
    print(f"Hypothesis report generated: {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
