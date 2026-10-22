import os
import unittest
from unittest.mock import Mock, patch

from scripts import calculate_scores


class ScoringFlowTests(unittest.TestCase):
    def run_flow(self, publish_digest, scoring_error=None, digest_error=None,
                 issues=None):
        calls = Mock()
        calls.project.return_value = ("project", "score", "repo")
        calls.fetch.return_value = issues or []
        calls.clear.side_effect = scoring_error
        calls.digest.side_effect = digest_error
        with (
            patch.dict(os.environ, {"PUBLISH_WEEKLY_DIGEST": publish_digest}),
            patch.object(calculate_scores, "TARGET_REPOS", ["example/repo"]),
            patch.object(calculate_scores, "get_project_and_field_ids", calls.project),
            patch.object(calculate_scores, "clear_project_board", calls.clear),
            patch.object(calculate_scores, "fetch_external_repo_issues", calls.fetch),
            patch.object(calculate_scores, "sync_to_github_project", calls.sync),
            patch.object(calculate_scores, "publish_weekly_digest", calls.digest)
        ):
            if scoring_error or digest_error:
                with self.assertRaises(RuntimeError):
                    calculate_scores.main()
            else:
                calculate_scores.main()
        return calls

    def test_scheduled_success_publishes_digest_after_refresh(self):
        calls = self.run_flow("true")
        self.assertEqual([call[0] for call in calls.mock_calls],
                         ["project", "clear", "fetch", "digest"])
        calls.digest.assert_called_once_with({"example/repo": []})

    def test_manual_success_does_not_publish_digest(self):
        calls = self.run_flow("false")
        self.assertEqual([call[0] for call in calls.mock_calls],
                         ["project", "clear", "fetch"])
        calls.digest.assert_not_called()

    def test_scoring_failure_does_not_publish_digest(self):
        calls = self.run_flow("true", scoring_error=RuntimeError("scoring failed"))
        calls.fetch.assert_not_called()
        calls.digest.assert_not_called()

    def test_digest_failure_is_propagated(self):
        calls = self.run_flow("true", digest_error=RuntimeError("digest failed"))
        calls.digest.assert_called_once()

    def test_qualifying_issue_is_synced_before_digest(self):
        issue = {"id": "issue-1", "number": 1, "repo_full_name": "example/repo"}
        calls = self.run_flow("true", issues=[issue])
        self.assertEqual([call[0] for call in calls.mock_calls],
                         ["project", "clear", "fetch", "sync", "digest"])
        calls.sync.assert_called_once_with(
            "project", "score", "repo", "issue-1", "example/repo", 0.0
        )
        calls.digest.assert_called_once_with({"example/repo": [(issue, 0.0)]})


if __name__ == "__main__":
    unittest.main()
