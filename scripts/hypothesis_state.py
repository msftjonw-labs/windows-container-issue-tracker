"""Durable research state stored in a repository-owned GitHub Actions issue."""

import json
import os
import re

if __package__:
    from .github_client import GitHubClient
else:
    from github_client import GitHubClient


STATE_TITLE = "[hypothesis-research-state] Weekly research lifecycle"
STATE_MARKER = "<!-- windows-container-hypothesis-state:v1 -->"
STATE_AUTHOR = "github-actions[bot]"
API_ROOT = "https://api.github.com"


class HypothesisStateStore:
    def __init__(self, repository, token):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("Research state repository must be owner/repository")
        if not token:
            raise ValueError("GITHUB_TOKEN is required for durable hypothesis state")
        self.url = f"{API_ROOT}/repos/{repository}/issues"
        self.client = GitHubClient(token)
        self.issue_number = None
        self.loaded_body = None

    @staticmethod
    def is_state_issue(issue):
        author = issue.get("user") or {}
        return (
            "pull_request" not in issue
            and issue.get("title") == STATE_TITLE
            and author.get("login") == STATE_AUTHOR
            and author.get("type") == "Bot"
        )

    def load(self):
        matches = []
        page = 1
        while True:
            response = self.client.request("GET", self.url, params={
                "state": "all", "creator": STATE_AUTHOR,
                "per_page": 100, "page": page,
            })
            issues = response.json()
            if not isinstance(issues, list):
                raise ValueError("Invalid GitHub research state issue listing")
            matches.extend(issue for issue in issues if self.is_state_issue(issue))
            if "next" not in response.links:
                break
            page += 1
        if len(matches) > 1:
            raise ValueError("Multiple research state issues found; resolve duplicates before retrying")
        if not matches:
            return None
        issue = matches[0]
        body = issue.get("body") or ""
        prefix = STATE_MARKER + "\n```json\n"
        if not body.startswith(prefix) or not body.endswith("\n```\n"):
            raise ValueError("Research state issue has an invalid state envelope")
        state = json.loads(body[len(prefix):-len("\n```\n")])
        if not isinstance(state, dict):
            raise ValueError("Research state must be a JSON object")
        self.issue_number = issue["number"]
        self.loaded_body = body
        return state

    def save(self, state):
        body = STATE_MARKER + "\n```json\n" + json.dumps(
            state, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ) + "\n```\n"
        if len(body) > 60000:
            raise ValueError("Research state exceeds safe GitHub issue body size")
        if self.issue_number is not None and body == self.loaded_body:
            return
        if self.issue_number is None:
            response = self.client.request(
                "POST", self.url, mutation=True, json={"title": STATE_TITLE, "body": body},
            )
            issue = response.json()
            if not self.is_state_issue(issue) or issue.get("body") != body:
                raise ValueError("GitHub did not confirm the new research state issue")
            self.issue_number = issue["number"]
        else:
            url = f"{self.url}/{self.issue_number}"
            current = self.client.request("GET", url).json()
            if not self.is_state_issue(current) or current.get("body") != self.loaded_body:
                raise ValueError("Research state changed during generation; retry without overwriting it")
            response = self.client.request("PATCH", url, mutation=True, json={"body": body})
            if response.json().get("body") != body:
                raise ValueError("GitHub did not confirm the research state update")
        self.loaded_body = body


def main():
    if __package__:
        from .generate_hypothesis_report import generate_report
    else:
        from generate_hypothesis_report import generate_report

    store = HypothesisStateStore(
        os.environ.get("GITHUB_REPOSITORY", ""), os.environ.get("GITHUB_TOKEN", ""),
    )
    state = store.load()
    output = generate_report(previous_state=state, on_success=store.save)
    print(f"Hypothesis report generated and research state saved: {output}")


if __name__ == "__main__":
    main()
