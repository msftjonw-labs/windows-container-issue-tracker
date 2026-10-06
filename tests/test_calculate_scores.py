import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

from scripts.calculate_scores import GraphQLError, fetch_external_repo_issues


class FetchExternalRepoIssuesTests(unittest.TestCase):
    @patch("scripts.calculate_scores.run_graphql")
    def test_skips_repository_blocked_by_classic_pat_policy(self, run_graphql):
        run_graphql.side_effect = GraphQLError(
            [
                {
                    "type": "FORBIDDEN",
                    "message": (
                        "The enterprise forbids access via a personal access tokens "
                        "(classic) if the token's lifetime is greater than 8 days."
                    ),
                }
            ]
        )
        output = StringIO()

        with redirect_stdout(output):
            issues = fetch_external_repo_issues("microsoft/Windows-Containers")

        self.assertEqual(issues, [])
        self.assertIn("Skipping 'microsoft/Windows-Containers'", output.getvalue())

    @patch("scripts.calculate_scores.run_graphql")
    def test_propagates_other_graphql_forbidden_errors(self, run_graphql):
        error = GraphQLError([{"type": "FORBIDDEN", "message": "Resource access denied"}])
        run_graphql.side_effect = error

        with self.assertRaises(GraphQLError) as raised:
            fetch_external_repo_issues("microsoft/Windows-Containers")

        self.assertIs(raised.exception, error)


if __name__ == "__main__":
    unittest.main()
