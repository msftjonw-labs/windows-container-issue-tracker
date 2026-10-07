import unittest
from unittest.mock import Mock, patch

from scripts import calculate_scores


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
