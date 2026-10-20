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


@patch.dict("os.environ", {
    "SMTP_HOST": "smtp.example.com",
    "SMTP_PORT": "465",
    "SMTP_SECURITY": "ssl",
    "SMTP_USERNAME": "sender@example.com",
    "SMTP_PASSWORD": "password",
    "WEEKLY_EMAIL_RECIPIENTS": "recipient@example.com"
}, clear=False)
class SmtpRetryTests(unittest.TestCase):
    def setUp(self):
        self.smtp_ssl = self.enterContext(
            patch("scripts.calculate_scores.smtplib.SMTP_SSL")
        )
        self.smtp_starttls = self.enterContext(
            patch("scripts.calculate_scores.smtplib.SMTP")
        )
        self.sleep = self.enterContext(
            patch("scripts.calculate_scores.time.sleep")
        )

    def test_retries_login_disconnect_with_a_fresh_tls_connection(self):
        for security, client in (
            ("ssl", self.smtp_ssl),
            ("starttls", self.smtp_starttls)
        ):
            with self.subTest(security=security), patch.dict(
                "os.environ", {"SMTP_SECURITY": security}
            ):
                first, second = Mock(), Mock()
                first.__enter__ = Mock(return_value=first)
                first.__exit__ = Mock(return_value=False)
                second.__enter__ = Mock(return_value=second)
                second.__exit__ = Mock(return_value=False)
                first.login.side_effect = (
                    calculate_scores.smtplib.SMTPServerDisconnected(
                        "Connection unexpectedly closed"
                    )
                )
                client.side_effect = [first, second]
                self.sleep.reset_mock()

                calculate_scores.send_weekly_email({})

                self.assertEqual(client.call_count, 2)
                first.send_message.assert_not_called()
                first.__exit__.assert_called_once()
                second.login.assert_called_once_with(
                    "sender@example.com", "password"
                )
                second.send_message.assert_called_once()
                second.__exit__.assert_called_once()
                if security == "starttls":
                    first.starttls.assert_called_once()
                    second.starttls.assert_called_once()
                self.sleep.assert_called_once_with(1)

    def test_retries_connection_timeout(self):
        connection = Mock()
        connection.__enter__ = Mock(return_value=connection)
        connection.__exit__ = Mock(return_value=False)
        self.smtp_ssl.side_effect = [TimeoutError(), connection]

        calculate_scores.send_weekly_email({})

        self.assertEqual(self.smtp_ssl.call_count, 2)
        connection.send_message.assert_called_once()
        self.sleep.assert_called_once_with(1)

    def test_stops_after_three_login_disconnects(self):
        smtp = self.smtp_ssl.return_value.__enter__.return_value
        smtp.login.side_effect = (
            calculate_scores.smtplib.SMTPServerDisconnected(
                "Connection unexpectedly closed"
            )
        )

        with self.assertRaises(calculate_scores.smtplib.SMTPServerDisconnected):
            calculate_scores.send_weekly_email({})

        self.assertEqual(self.smtp_ssl.call_count, 3)
        self.assertEqual([call.args[0] for call in self.sleep.call_args_list], [1, 2])
        smtp.send_message.assert_not_called()

    def test_does_not_retry_authentication_errors(self):
        smtp = self.smtp_ssl.return_value.__enter__.return_value
        smtp.login.side_effect = calculate_scores.smtplib.SMTPAuthenticationError(
            535, b"Authentication failed"
        )

        with self.assertRaises(calculate_scores.smtplib.SMTPAuthenticationError):
            calculate_scores.send_weekly_email({})

        self.smtp_ssl.assert_called_once()
        self.sleep.assert_not_called()
        smtp.send_message.assert_not_called()

    def test_does_not_retry_disconnect_during_delivery(self):
        smtp = self.smtp_ssl.return_value.__enter__.return_value
        smtp.send_message.side_effect = (
            calculate_scores.smtplib.SMTPServerDisconnected(
                "Connection unexpectedly closed"
            )
        )

        with self.assertRaises(calculate_scores.smtplib.SMTPServerDisconnected):
            calculate_scores.send_weekly_email({})

        self.smtp_ssl.assert_called_once()
        self.sleep.assert_not_called()
        smtp.send_message.assert_called_once()


class SendWeeklyEmailTests(unittest.TestCase):
    @patch("scripts.calculate_scores.ssl.create_default_context")
    @patch("scripts.calculate_scores.smtplib.SMTP_SSL")
    @patch.dict("os.environ", {
        "SMTP_HOST": "smtp.gmail.com",
        "SMTP_PORT": "465",
        "SMTP_SECURITY": "ssl",
        "SMTP_USERNAME": "sender@example.com",
        "SMTP_PASSWORD": "app-password",
        "SMTP_FROM_ADDRESS": "digest@example.com",
        "WEEKLY_EMAIL_RECIPIENTS": "one@example.com,two@example.com"
    }, clear=False)
    def test_sends_html_digest_over_implicit_tls(self, smtp_ssl, context):
        smtp = smtp_ssl.return_value.__enter__.return_value

        calculate_scores.send_weekly_email({}, "https://example.com/digest")

        smtp_ssl.assert_called_once_with(
            "smtp.gmail.com",
            465,
            context=context.return_value,
            timeout=30
        )
        smtp.login.assert_called_once_with(
            "sender@example.com",
            "app-password"
        )
        message = smtp.send_message.call_args.args[0]
        self.assertEqual(message["From"], "digest@example.com")
        self.assertEqual(
            message["To"],
            "one@example.com, two@example.com"
        )
        self.assertIn(
            "https://example.com/digest",
            message.get_body(preferencelist=("html",)).get_content()
        )

    @patch("scripts.calculate_scores.ssl.create_default_context")
    @patch("scripts.calculate_scores.smtplib.SMTP")
    @patch.dict("os.environ", {
        "SMTP_HOST": "smtp.example.com",
        "SMTP_PORT": "587",
        "SMTP_SECURITY": "starttls",
        "SMTP_USERNAME": "sender@example.com",
        "SMTP_PASSWORD": "password",
        "WEEKLY_EMAIL_RECIPIENTS": "recipient@example.com"
    }, clear=False)
    def test_sends_digest_over_starttls(self, smtp_client, context):
        smtp = smtp_client.return_value.__enter__.return_value

        calculate_scores.send_weekly_email({})

        smtp_client.assert_called_once_with(
            "smtp.example.com",
            587,
            timeout=30
        )
        smtp.starttls.assert_called_once_with(context=context.return_value)
        smtp.login.assert_called_once_with(
            "sender@example.com",
            "password"
        )
        smtp.send_message.assert_called_once()

    @patch.dict("os.environ", {
        "SMTP_SECURITY": "none",
        "SMTP_USERNAME": "sender@example.com",
        "SMTP_PASSWORD": "password",
        "WEEKLY_EMAIL_RECIPIENTS": "recipient@example.com"
    }, clear=False)
    def test_rejects_unencrypted_smtp(self):
        with self.assertRaisesRegex(
            ValueError,
            "SMTP_SECURITY must be either 'ssl' or 'starttls'"
        ):
            calculate_scores.send_weekly_email({})


if __name__ == "__main__":
    unittest.main()
