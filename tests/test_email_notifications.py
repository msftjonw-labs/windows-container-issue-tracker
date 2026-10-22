import os
import smtplib
import ssl
import unittest
from unittest.mock import Mock, patch

from scripts import calculate_scores
from scripts import send_success_email


class SuccessEmailTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    @patch("scripts.send_success_email.smtplib.SMTP")
    def test_missing_host_skips_email(self, smtp):
        send_success_email.send_success_email()
        smtp.assert_not_called()

    @patch("scripts.send_success_email.smtplib.SMTP_SSL")
    def test_missing_sender_or_recipient_fails_before_connecting(self, smtp):
        os.environ["SMTP_HOST"] = "smtp.example.com"
        with self.assertRaisesRegex(ValueError, "SMTP_FROM and SMTP_TO"):
            send_success_email.send_success_email()
        smtp.assert_not_called()

    @patch("scripts.send_success_email.smtplib.SMTP_SSL")
    def test_sends_over_verified_implicit_tls(self, smtp):
        os.environ.update({
            "SMTP_HOST": "smtp.example.com",
            "SMTP_FROM": "sender@example.com",
            "SMTP_TO": "recipient@example.com, second@example.com",
            "SMTP_SECURITY": "ssl",
            "SMTP_PORT": "2465",
            "SMTP_USERNAME": "sender@example.com",
            "SMTP_PASSWORD": "test-only-password",
            "GITHUB_REPOSITORY": "example/repo",
            "GITHUB_RUN_ID": "1"
        })
        smtp.return_value.__enter__.return_value.send_message.return_value = {}
        send_success_email.send_success_email()

        self.assertEqual(smtp.call_args.args, ("smtp.example.com", 2465))
        context = smtp.call_args.kwargs["context"]
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertEqual(smtp.call_args.kwargs["timeout"], 30)
        connection = smtp.return_value.__enter__.return_value
        connection.login.assert_called_once_with(
            "sender@example.com", "test-only-password"
        )
        connection.starttls.assert_not_called()
        message = connection.send_message.call_args.args[0]
        self.assertEqual(
            message["To"], "recipient@example.com, second@example.com"
        )
        self.assertEqual(message["From"], "sender@example.com")
        body = message.get_body(preferencelist=("plain",)).get_content()
        self.assertIn("example/repo", body)
        self.assertIn("https://github.com/example/repo/actions/runs/1", body)

    @patch("scripts.send_success_email.smtplib.SMTP")
    def test_sends_over_starttls_with_default_port(self, smtp):
        os.environ.update({
            "SMTP_HOST": "smtp.example.com",
            "SMTP_FROM": "sender@example.com",
            "SMTP_TO": "recipient@example.com",
            "SMTP_SECURITY": "starttls",
            "SMTP_USERNAME": "sender@example.com",
            "SMTP_PASSWORD": "test-only-password"
        })
        smtp.return_value.__enter__.return_value.send_message.return_value = {}
        send_success_email.send_success_email()

        self.assertEqual(smtp.call_args.args, ("smtp.example.com", 587))
        connection = smtp.return_value.__enter__.return_value
        connection.starttls.assert_called_once()
        connection.login.assert_called_once_with(
            "sender@example.com", "test-only-password"
        )

    @patch("scripts.send_success_email.smtplib.SMTP_SSL")
    def test_smtp_disconnect_is_propagated_for_main_to_handle(self, smtp):
        os.environ.update({
            "SMTP_HOST": "smtp.example.com",
            "SMTP_FROM": "sender@example.com",
            "SMTP_TO": "recipient@example.com",
            "SMTP_SECURITY": "ssl",
            "SMTP_USERNAME": "sender@example.com",
            "SMTP_PASSWORD": "test-only-password"
        })
        connection = smtp.return_value.__enter__.return_value
        connection.login.side_effect = smtplib.SMTPServerDisconnected(
            "Connection unexpectedly closed"
        )
        with self.assertRaises(smtplib.SMTPServerDisconnected):
            send_success_email.send_success_email()
        connection.send_message.assert_not_called()


class SuccessEmailFlowTests(unittest.TestCase):
    def run_flow(self, publish_digest, scoring_error=None, digest_error=None,
                 email_error=None):
        calls = Mock()
        calls.project.return_value = ("project", "score", "repo")
        calls.fetch.return_value = []
        calls.clear.side_effect = scoring_error
        calls.digest.side_effect = digest_error
        calls.email.side_effect = email_error
        with (
            patch.dict(os.environ, {"PUBLISH_WEEKLY_DIGEST": publish_digest}),
            patch.object(calculate_scores, "TARGET_REPOS", ["example/repo"]),
            patch.object(calculate_scores, "get_project_and_field_ids", calls.project),
            patch.object(calculate_scores, "clear_project_board", calls.clear),
            patch.object(calculate_scores, "fetch_external_repo_issues", calls.fetch),
            patch.object(calculate_scores, "publish_weekly_digest", calls.digest),
            patch.object(calculate_scores, "send_success_email", calls.email)
        ):
            if scoring_error or digest_error:
                with self.assertRaises(RuntimeError):
                    calculate_scores.main()
            else:
                calculate_scores.main()
        return calls

    def test_scheduled_success_sends_email_after_digest(self):
        calls = self.run_flow("true")
        self.assertEqual([call[0] for call in calls.mock_calls],
                         ["project", "clear", "fetch", "digest", "email"])
        calls.email.assert_called_once_with()

    def test_manual_success_also_sends_email(self):
        calls = self.run_flow("false")
        calls.digest.assert_not_called()
        calls.email.assert_called_once_with()

    def test_scoring_failure_does_not_send_email(self):
        calls = self.run_flow("true", scoring_error=RuntimeError("scoring failed"))
        calls.email.assert_not_called()
        calls.digest.assert_not_called()

    def test_digest_failure_does_not_send_email(self):
        calls = self.run_flow("true", digest_error=RuntimeError("digest failed"))
        calls.email.assert_not_called()

    def test_email_failure_does_not_fail_scoring(self):
        calls = self.run_flow(
            "false",
            email_error=smtplib.SMTPServerDisconnected(
                "Connection unexpectedly closed"
            )
        )
        calls.email.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
