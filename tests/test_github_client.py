import unittest
from unittest.mock import Mock, patch

import requests

from scripts.github_client import GitHubClient


class GitHubClientTests(unittest.TestCase):
    def setUp(self):
        self.client = GitHubClient("test-only-placeholder")
        self.request = patch("scripts.github_client.requests.request").start()
        self.sleep = patch("scripts.github_client.time.sleep").start()
        self.clock = patch("scripts.github_client.time.time", return_value=1000).start()
        self.addCleanup(patch.stopall)

    @staticmethod
    def response(status=200, headers=None, payload=None, text=""):
        response = Mock(status_code=status, headers=headers or {}, text=text)
        response.json.return_value = payload or {"data": {"viewer": {"login": "example"}}}
        if status >= 400:
            response.raise_for_status.side_effect = requests.HTTPError(str(status))
        return response

    def test_primary_rate_limit_uses_reset_time(self):
        self.request.side_effect = [
            self.response(403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1120"}),
            self.response(),
        ]
        self.client.request("GET", "https://api.github.com/repos/example/repo/issues")
        self.sleep.assert_called_once_with(121)
        self.assertEqual(self.request.call_count, 2)
        self.assertEqual(self.request.call_args.kwargs["timeout"], 30)

    def test_secondary_limit_honors_retry_after(self):
        self.request.side_effect = [
            self.response(429, {"Retry-After": "75"}), self.response(),
        ]
        self.client.request("GET", "https://api.github.com/test")
        self.sleep.assert_called_once_with(75)

    def test_secondary_limit_without_header_backs_off(self):
        self.request.side_effect = [
            self.response(403, text="You have exceeded a secondary rate limit."),
            self.response(403, text="You have exceeded a secondary rate limit."),
            self.response(),
        ]
        self.client.request("GET", "https://api.github.com/test")
        self.assertEqual([call.args[0] for call in self.sleep.call_args_list], [60, 120])

    def test_graphql_http_200_rate_limit_is_retried(self):
        self.request.side_effect = [
            self.response(payload={"errors": [{"type": "RATE_LIMITED", "message": "API rate limit exceeded"}]}),
            self.response(),
        ]
        result = self.client.request("POST", "https://api.github.com/graphql", graphql=True)
        self.assertEqual(result, {"viewer": {"login": "example"}})
        self.sleep.assert_called_once_with(60)

    def test_permission_error_is_not_retried(self):
        self.request.return_value = self.response(403, text="Resource not accessible by integration")
        with self.assertRaises(requests.HTTPError):
            self.client.request("GET", "https://api.github.com/test")
        self.request.assert_called_once()
        self.sleep.assert_not_called()

    def test_low_budget_waits_before_next_request(self):
        self.request.side_effect = [
            self.response(headers={"X-RateLimit-Remaining": "5", "X-RateLimit-Reset": "1100"}),
            self.response(),
        ]
        self.client.request("GET", "https://api.github.com/test")
        self.sleep.assert_not_called()
        self.client.request("GET", "https://api.github.com/test")
        self.sleep.assert_called_once_with(101)

    def test_rest_and_graphql_budgets_are_independent(self):
        self.request.side_effect = [
            self.response(headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1100"}),
            self.response(),
        ]
        self.client.request("GET", "https://api.github.com/test")
        self.client.request("POST", "https://api.github.com/graphql", graphql=True)
        self.sleep.assert_not_called()

    def test_long_reset_fails_without_sleeping_or_retrying(self):
        self.request.return_value = self.response(
            403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "5000"}
        )
        with self.assertRaisesRegex(RuntimeError, "wait budget exhausted"):
            self.client.request("GET", "https://api.github.com/test")
        self.sleep.assert_not_called()
        self.request.assert_called_once()

    def test_wait_budget_is_cumulative_across_requests(self):
        self.client.wait(600)
        with self.assertRaisesRegex(RuntimeError, "wait budget exhausted"):
            self.client.wait(301)
        self.sleep.assert_called_once_with(600)

    def test_rate_limit_retries_are_bounded(self):
        self.request.return_value = self.response(429, {"Retry-After": "1"})
        with self.assertRaisesRegex(RuntimeError, "attempts exhausted"):
            self.client.request("GET", "https://api.github.com/test")
        self.assertEqual(self.request.call_count, 3)
        self.assertEqual(self.sleep.call_count, 2)

    def test_retry_after_date_and_malformed_header(self):
        self.assertEqual(self.client.retry_after({"Retry-After": "Thu, 01 Jan 1970 00:18:20 GMT"}), 100)
        self.assertIsNone(self.client.retry_after({"Retry-After": "invalid"}))

    def test_reads_retry_network_timeout(self):
        self.request.side_effect = [requests.Timeout(), self.response()]
        self.client.request("GET", "https://api.github.com/test")
        self.sleep.assert_called_once_with(1)

    def test_mutations_do_not_replay_network_timeout(self):
        self.request.side_effect = requests.Timeout()
        with self.assertRaises(requests.Timeout):
            self.client.request("POST", "https://api.github.com/test", mutation=True)
        self.request.assert_called_once()

    def test_rest_writes_do_not_replay_ambiguous_server_errors(self):
        self.request.return_value = self.response(502)
        with self.assertRaisesRegex(RuntimeError, "write outcome unknown"):
            self.client.request("POST", "https://api.github.com/test", mutation=True)
        self.request.assert_called_once()

    def test_partial_graphql_mutation_is_not_replayed(self):
        self.request.return_value = self.response(payload={
            "data": {"score": {"id": "item-1"}, "source": None},
            "errors": [{"message": "Something went wrong while executing your query."}],
        })
        with self.assertRaisesRegex(RuntimeError, "partial mutation"):
            self.client.request("POST", "https://api.github.com/graphql", graphql=True, mutation=True)
        self.request.assert_called_once()

    def test_mutations_are_paced(self):
        self.request.return_value = self.response()
        with patch("scripts.github_client.time.monotonic", return_value=10):
            self.client.request("POST", "https://api.github.com/test", mutation=True)
            self.client.request("POST", "https://api.github.com/test", mutation=True)
        self.sleep.assert_called_once_with(1)

    def test_successful_rest_content_containing_timeout_is_not_retried(self):
        response = self.response(text='[{"title": "Fix timeout"}]')
        self.request.return_value = response
        self.assertIs(self.client.request("GET", "https://api.github.com/test"), response)
        self.request.assert_called_once()
