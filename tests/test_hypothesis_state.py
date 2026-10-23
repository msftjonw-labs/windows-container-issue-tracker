import json
import unittest
from unittest.mock import Mock, patch

from scripts import hypothesis_state


class HypothesisStateTests(unittest.TestCase):
    def setUp(self):
        self.client_patch = patch("scripts.hypothesis_state.GitHubClient")
        self.client = self.client_patch.start().return_value
        self.addCleanup(self.client_patch.stop)
        self.store = hypothesis_state.HypothesisStateStore("example/repo", "test-only-placeholder")

    @staticmethod
    def response(data, more=False):
        return Mock(json=Mock(return_value=data), links={"next": {}} if more else {})

    @staticmethod
    def issue(state=None, **overrides):
        state = {"version": 1} if state is None else state
        issue = {
            "number": 42,
            "title": hypothesis_state.STATE_TITLE,
            "user": {"login": "github-actions[bot]", "type": "Bot"},
            "body": hypothesis_state.STATE_MARKER + "\n```json\n" + json.dumps(state) + "\n```\n",
        }
        issue.update(overrides)
        return issue

    def test_validates_configuration(self):
        for repo in ("", "repo", "../example/repo", "example/repo?x=1"):
            with self.subTest(repo=repo), self.assertRaises(ValueError):
                hypothesis_state.HypothesisStateStore(repo, "test-only-placeholder")
        with self.assertRaisesRegex(ValueError, "GITHUB_TOKEN"):
            hypothesis_state.HypothesisStateStore("example/repo", "")

    def test_loads_state_from_later_page_including_closed_issue(self):
        self.client.request.side_effect = [
            self.response([], more=True), self.response([self.issue()]),
        ]
        self.assertEqual(self.store.load(), {"version": 1})
        self.assertEqual(self.store.issue_number, 42)
        self.assertEqual(self.client.request.call_args.kwargs["params"]["page"], 2)
        self.assertEqual(self.client.request.call_args.kwargs["params"]["state"], "all")

    def test_ignores_spoofed_title_or_pull_request(self):
        self.client.request.return_value = self.response([
            self.issue(user={"login": "someone", "type": "User"}),
            self.issue(pull_request={}),
            self.issue(title="An ordinary issue"),
        ])
        self.assertIsNone(self.store.load())

    def test_does_not_reset_malformed_state(self):
        for body in ("not JSON", hypothesis_state.STATE_MARKER + "\n```json\n[]\n```\n"):
            self.client.request.return_value = self.response([self.issue(body=body)])
            with self.subTest(body=body), self.assertRaises(ValueError):
                self.store.load()

    def test_duplicate_state_is_an_error(self):
        self.client.request.return_value = self.response([self.issue(), self.issue(number=43)])
        with self.assertRaisesRegex(ValueError, "Multiple"):
            self.store.load()

    def test_api_failure_propagates(self):
        self.client.request.side_effect = RuntimeError("permission denied")
        with self.assertRaisesRegex(RuntimeError, "permission denied"):
            self.store.load()

    def test_creates_state_with_scoped_client(self):
        def request(method, url, **kwargs):
            self.assertEqual(method, "POST")
            self.assertTrue(kwargs["mutation"])
            return self.response(self.issue(body=kwargs["json"]["body"]))
        self.client.request.side_effect = request
        self.store.save({"version": 1})
        self.assertEqual(self.store.issue_number, 42)

    def test_updates_only_unchanged_bot_owned_state(self):
        issue = self.issue()
        self.client.request.return_value = self.response([issue])
        self.store.load()
        def request(method, url, **kwargs):
            if method == "GET":
                return self.response(issue)
            self.assertEqual(method, "PATCH")
            self.assertTrue(kwargs["mutation"])
            return self.response({"body": kwargs["json"]["body"]})
        self.client.request.side_effect = request
        self.store.save({"version": 1, "week": "2026-10-19"})
        self.assertIn("2026-10-19", self.store.loaded_body)

    def test_refuses_concurrent_edit(self):
        self.client.request.return_value = self.response([self.issue()])
        self.store.load()
        self.client.request.return_value = self.response(self.issue(body="manual edit"))
        with self.assertRaisesRegex(ValueError, "changed"):
            self.store.save({"version": 1})
        self.assertEqual(self.client.request.call_args.args[0], "GET")

    def test_unchanged_weekly_state_does_not_write(self):
        def request(method, url, **kwargs):
            return self.response(self.issue(body=kwargs["json"]["body"]))
        self.client.request.side_effect = request
        self.store.save({"version": 1})
        self.client.request.reset_mock()
        self.store.save({"version": 1})
        self.client.request.assert_not_called()

    def test_refuses_unconfirmed_or_oversized_write(self):
        self.client.request.return_value = self.response({})
        with self.assertRaisesRegex(ValueError, "confirm"):
            self.store.save({"version": 1})
        self.client.request.reset_mock()
        with self.assertRaisesRegex(ValueError, "size"):
            self.store.save({"huge": "x" * 60000})
        self.client.request.assert_not_called()

    @patch("scripts.generate_hypothesis_report.generate_report")
    @patch("scripts.hypothesis_state.HypothesisStateStore")
    def test_runner_passes_loaded_state_and_save_callback(self, store_class, generate):
        store = store_class.return_value
        store.load.return_value = {"version": 1}
        with patch.dict("os.environ", {
            "GITHUB_REPOSITORY": "example/repo", "GITHUB_TOKEN": "test-only-placeholder",
        }, clear=True):
            hypothesis_state.main()
        generate.assert_called_once_with(previous_state={"version": 1}, on_success=store.save)


if __name__ == "__main__":
    unittest.main()
