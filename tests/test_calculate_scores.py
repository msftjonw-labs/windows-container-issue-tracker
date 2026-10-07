import unittest
from unittest.mock import Mock, patch

from scripts import calculate_scores


class FetchExternalRepoIssuesTests(unittest.TestCase):
    @staticmethod
    def page(nodes, has_next_page=False, cursor=None):
        return {
            "repository": {
                "issues": {
                    "nodes": nodes,
                    "pageInfo": {"hasNextPage": has_next_page, "endCursor": cursor}
                }
            }
        }

    @patch("scripts.calculate_scores.run_graphql")
    def test_paginates_and_preserves_exact_label_filter_and_scores(self, graphql):
        matching_issue = {
            "id": "issue-2",
            "labels": {"nodes": [{"name": "sig/windows"}, {"name": "bug"}]},
            "comments": {"nodes": [
                {"author": {"login": "user"}},
                {"author": {"login": "user"}},
                {"author": None}
            ]}
        }
        graphql.side_effect = [
            self.page([
                {"id": "issue-1", "labels": {"nodes": [{"name": "sig/windows-extra"}]}}
            ], True, "next-page"),
            self.page([matching_issue])
        ]

        issues = calculate_scores.fetch_external_repo_issues("kubernetes/kubernetes")

        self.assertEqual(issues, [matching_issue])
        self.assertEqual(calculate_scores.compute_priority_score(issues[0]), 11.0)
        self.assertEqual(graphql.call_count, 2)
        self.assertEqual(graphql.call_args_list[0].args[1], {
            "owner": "kubernetes", "repo": "kubernetes", "first": 10, "after": None
        })
        self.assertEqual(graphql.call_args_list[1].args[1], {
            "owner": "kubernetes", "repo": "kubernetes", "first": 10, "after": "next-page"
        })
        query = graphql.call_args.args[0]
        self.assertIn("first: $first, after: $after", query)
        self.assertIn("orderBy: {field: UPDATED_AT, direction: DESC}", query)
        self.assertIn("comments(first: 100)", query)
        self.assertIn("labels(first: 50)", query)

    @patch("scripts.calculate_scores.run_graphql")
    def test_stops_at_100_issues_even_when_none_match(self, graphql):
        nodes = [{"id": f"issue-{i}", "labels": {"nodes": []}} for i in range(100)]
        graphql.side_effect = [
            self.page(nodes[i:i + 10], True, f"cursor-{i + 10}")
            for i in range(0, 100, 10)
        ]

        self.assertEqual(
            calculate_scores.fetch_external_repo_issues("kubernetes/kubernetes"), []
        )

        self.assertEqual(graphql.call_count, 10)
        self.assertTrue(all(call.args[1]["first"] == 10 for call in graphql.call_args_list))
        self.assertEqual(graphql.call_args.args[1]["after"], "cursor-90")

    @patch("scripts.calculate_scores.run_graphql")
    def test_no_label_requirement_preserves_issue_order_across_pages(self, graphql):
        nodes = [{"id": f"issue-{i}"} for i in range(12)]
        graphql.side_effect = [
            self.page(nodes[:10], True, "next-page"),
            self.page(nodes[10:])
        ]

        self.assertEqual(
            calculate_scores.fetch_external_repo_issues("microsoft/windows-containers"),
            nodes
        )
        self.assertEqual(graphql.call_count, 2)

    @patch("scripts.calculate_scores.run_graphql")
    def test_empty_or_missing_repository(self, graphql):
        for response in (None, {"repository": None}, self.page([])):
            with self.subTest(response=response):
                graphql.reset_mock()
                graphql.return_value = response

                self.assertEqual(
                    calculate_scores.fetch_external_repo_issues("kubernetes/kubernetes"),
                    []
                )
                graphql.assert_called_once()

    @patch("scripts.calculate_scores.run_graphql")
    def test_page_failure_is_not_silently_skipped(self, graphql):
        graphql.side_effect = [
            self.page([{"id": "issue-1"}], True, "next-page"),
            Exception("GraphQL query failed (504)")
        ]

        with self.assertRaisesRegex(Exception, "GraphQL query failed"):
            calculate_scores.fetch_external_repo_issues("microsoft/windows-containers")


class RunGraphqlTests(unittest.TestCase):
    @patch("scripts.calculate_scores.time.sleep")
    @patch("scripts.calculate_scores.requests.post")
    def test_retries_transient_gateway_timeout(self, post, sleep):
        timeout_response = Mock()
        timeout_response.status_code = 504
        timeout_response.text = "Gateway Timeout"
        success_response = Mock()
        success_response.status_code = 200
        success_response.json.return_value = {"data": {"viewer": {"login": "user"}}}
        post.side_effect = [timeout_response, success_response]

        result = calculate_scores.run_graphql("query { viewer { login } }")

        self.assertEqual(result, {"viewer": {"login": "user"}})
        self.assertEqual(post.call_count, 2)
        sleep.assert_called_once_with(1)

    @patch("scripts.calculate_scores.time.sleep")
    @patch("scripts.calculate_scores.requests.post")
    def test_retries_transient_internal_error(self, post, sleep):
        transient_response = Mock()
        transient_response.status_code = 200
        transient_response.json.return_value = {
            "errors": [{"message": "Something went wrong while executing your query."}]
        }
        success_response = Mock()
        success_response.status_code = 200
        success_response.json.return_value = {"data": {"viewer": {"login": "user"}}}
        post.side_effect = [transient_response, success_response]

        result = calculate_scores.run_graphql("query { viewer { login } }")

        self.assertEqual(result, {"viewer": {"login": "user"}})
        self.assertEqual(post.call_count, 2)
        sleep.assert_called_once_with(1)

    @patch("scripts.calculate_scores.time.sleep")
    @patch("scripts.calculate_scores.requests.post")
    def test_does_not_retry_non_transient_error(self, post, sleep):
        response = Mock()
        response.status_code = 200
        response.json.return_value = {
            "errors": [{"message": "Resource not accessible by integration"}]
        }
        post.return_value = response

        with self.assertRaisesRegex(Exception, "Resource not accessible by integration"):
            calculate_scores.run_graphql("query { viewer { login } }")

        post.assert_called_once()
        sleep.assert_not_called()

    @patch("scripts.calculate_scores.time.sleep")
    @patch("scripts.calculate_scores.requests.post")
    def test_stops_after_maximum_attempts(self, post, sleep):
        response = Mock()
        response.status_code = 200
        response.json.return_value = {
            "errors": [{"message": "Something went wrong while executing your query."}]
        }
        post.return_value = response

        with self.assertRaisesRegex(Exception, "Something went wrong"):
            calculate_scores.run_graphql("query { viewer { login } }")

        self.assertEqual(post.call_count, calculate_scores.MAX_GRAPHQL_ATTEMPTS)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2])


if __name__ == "__main__":
    unittest.main()
