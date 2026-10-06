import unittest
from unittest.mock import Mock, patch

from scripts import calculate_scores


class RunGraphqlTests(unittest.TestCase):
    def test_retries_transient_graphql_error(self):
        transient_error = Mock(status_code=200)
        transient_error.json.return_value = {
            "errors": [{"message": "Something went wrong while executing your query"}]
        }
        success = Mock(status_code=200)
        success.json.return_value = {"data": {"ok": True}}

        with (
            patch.object(calculate_scores.requests, "post", side_effect=[transient_error, success]) as post,
            patch.object(calculate_scores.time, "sleep") as sleep,
        ):
            result = calculate_scores.run_graphql("query")

        self.assertEqual(result, {"ok": True})
        self.assertEqual(post.call_count, 2)
        sleep.assert_called_once_with(1)

    def test_does_not_retry_permanent_graphql_error(self):
        response = Mock(status_code=200)
        response.json.return_value = {"errors": [{"message": "Resource not accessible by integration"}]}

        with (
            patch.object(calculate_scores.requests, "post", return_value=response) as post,
            patch.object(calculate_scores.time, "sleep") as sleep,
        ):
            with self.assertRaisesRegex(Exception, "Resource not accessible"):
                calculate_scores.run_graphql("query")

        post.assert_called_once()
        sleep.assert_not_called()

    def test_retries_server_error_status(self):
        server_error = Mock(status_code=503, text="Service unavailable")
        success = Mock(status_code=200)
        success.json.return_value = {"data": {"ok": True}}

        with (
            patch.object(calculate_scores.requests, "post", side_effect=[server_error, success]) as post,
            patch.object(calculate_scores.time, "sleep") as sleep,
        ):
            result = calculate_scores.run_graphql("query")

        self.assertEqual(result, {"ok": True})
        self.assertEqual(post.call_count, 2)
        sleep.assert_called_once_with(1)

    def test_stops_after_maximum_retries(self):
        response = Mock(status_code=200)
        response.json.return_value = {
            "errors": [{"message": "Something went wrong while executing your query"}]
        }

        with (
            patch.object(calculate_scores.requests, "post", return_value=response) as post,
            patch.object(calculate_scores.time, "sleep") as sleep,
        ):
            with self.assertRaisesRegex(Exception, "Something went wrong"):
                calculate_scores.run_graphql("query")

        self.assertEqual(post.call_count, calculate_scores.MAX_GRAPHQL_RETRIES + 1)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2, 4])


if __name__ == "__main__":
    unittest.main()
