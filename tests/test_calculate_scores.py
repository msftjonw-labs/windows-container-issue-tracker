import unittest
from unittest.mock import Mock, patch

from scripts import calculate_scores


class FetchExternalRepoIssuesTests(unittest.TestCase):
    @staticmethod
    def response(issues, has_next_page=False, status_code=200):
        response = Mock()
        response.status_code = status_code
        response.json.return_value = issues
        response.links = {"next": {"url": "next-page"}} if has_next_page else {}
        response.text = ""
        return response

    @staticmethod
    def details(node_id):
        return {
            "id": node_id,
            "comments": {
                "nodes": [
                    {"createdAt": "2099-01-01T00:00:00Z", "author": {"login": "user-1"}},
                    {"createdAt": "2099-01-02T00:00:00Z", "author": {"login": "user-2"}}
                ]
            }
        }

    @patch("scripts.calculate_scores.fetch_issue_graphql_details")
    @patch("scripts.calculate_scores.requests.get")
    def test_paginates_beyond_100_issues_without_result_cap(self, get, details):
        first_page = [
            {"number": number, "node_id": f"issue-{number}", "labels": []}
            for number in range(1, 101)
        ]
        second_page = [{"number": 101, "node_id": "issue-101", "labels": []}]
        get.side_effect = [
            self.response(first_page, has_next_page=True),
            self.response(second_page)
        ]
        details.side_effect = lambda node_id: self.details(node_id)

        issues = calculate_scores.fetch_external_repo_issues(
            "microsoft/windows-containers"
        )

        self.assertEqual(len(issues), 101)
        self.assertEqual(details.call_count, 101)
        self.assertEqual(get.call_count, 2)
        self.assertEqual(get.call_args_list[0].kwargs["params"]["page"], 1)
        self.assertEqual(get.call_args_list[1].kwargs["params"]["page"], 2)
        self.assertEqual(
            get.call_args_list[0].kwargs["params"]["since"],
            calculate_scores.ISSUE_LOOKBACK_TIMESTAMP
        )
        self.assertEqual(calculate_scores.ISSUE_LOOKBACK_DAYS, 1825)

    @patch("scripts.calculate_scores.fetch_issue_graphql_details")
    @patch("scripts.calculate_scores.requests.get")
    def test_aks_accepts_only_windows_or_win_labels_case_insensitively(
        self, get, details
    ):
        get.return_value = self.response([
            {"number": 1, "node_id": "windows", "labels": [{"name": "WIndows"}]},
            {"number": 2, "node_id": "win", "labels": [{"name": "wIN"}]},
            {
                "number": 3,
                "node_id": "windows-server",
                "labels": [{"name": "windows-server"}]
            },
            {"number": 4, "node_id": "darwin", "labels": [{"name": "darwin"}]}
        ])
        details.side_effect = lambda node_id: self.details(node_id)

        issues = calculate_scores.fetch_external_repo_issues("Azure/AKS")

        self.assertEqual([issue["id"] for issue in issues], ["windows", "win"])
        self.assertEqual(details.call_count, 2)


class SyncToGithubProjectTests(unittest.TestCase):
    @patch("scripts.calculate_scores.run_graphql")
    def test_adds_issue_with_supported_mutation_then_updates_score(self, graphql):
        graphql.side_effect = [
            {"addProjectV2ItemById": {"item": {"id": "item-1"}}},
            {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "item-1"}}}
        ]

        calculate_scores.sync_to_github_project("project-1", "field-1", "issue-1", 11.5)

        self.assertEqual(graphql.call_count, 2)
        add_call, update_call = graphql.call_args_list
        self.assertIn(
            "addProjectV2ItemById(input: {projectId: $projectId, contentId: $contentId})",
            add_call.args[0]
        )
        self.assertNotIn("addProjectV2ItemByNodeId", add_call.args[0])
        self.assertEqual(add_call.args[1], {
            "projectId": "project-1", "contentId": "issue-1"
        })
        self.assertIn("updateProjectV2ItemFieldValue(", update_call.args[0])
        self.assertIn("value: { number: $value }", update_call.args[0])
        self.assertEqual(update_call.args[1], {
            "projectId": "project-1",
            "itemId": "item-1",
            "fieldId": "field-1",
            "value": 11.5
        })

    @patch("scripts.calculate_scores.run_graphql")
    def test_add_failure_does_not_attempt_score_update(self, graphql):
        graphql.side_effect = Exception("GraphQL Errors: undefinedField")

        with self.assertRaisesRegex(Exception, "undefinedField"):
            calculate_scores.sync_to_github_project("project-1", "field-1", "issue-1", 11.5)

        graphql.assert_called_once()


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
