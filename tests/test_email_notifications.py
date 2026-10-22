import os
import smtplib
import ssl
import unittest
from unittest.mock import Mock, patch

from scripts import calculate_scores


class SuccessEmailTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    @patch("scripts.calculate_scores.smtplib.SMTP_SSL")
    def test_missing_recipient_skips_email(self, smtp):
        calculate_scores.send_success_email({})
        smtp.assert_not_called()

    @patch("scripts.calculate_scores.smtplib.SMTP_SSL")
    def test_missing_credentials_fails_before_connecting(self, smtp):
        os.environ["EMAIL_TO"] = "recipient@example.com"
        with self.assertRaisesRegex(ValueError, "SMTP_USERNAME and SMTP_PASSWORD"):
            calculate_scores.send_success_email({})
        smtp.assert_not_called()

    @patch("scripts.calculate_scores.smtplib.SMTP_SSL")
    def test_sends_summary_over_verified_tls(self, smtp):
        os.environ.update({
            "EMAIL_TO": "recipient@example.com",
            "SMTP_USERNAME": "sender@example.com",
            "SMTP_PASSWORD": "test-only-password",
            "GITHUB_WORKFLOW_RUN_URL": "https://github.com/example/repo/actions/runs/1",
            "SMTP_SERVER": "",
            "SMTP_PORT": ""
        })
        calculate_scores.send_success_email({"example/repo": []})

        self.assertEqual(smtp.call_args.args, ("smtp.gmail.com", 465))
        context = smtp.call_args.kwargs["context"]
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertEqual(smtp.call_args.kwargs["timeout"], 30)
        connection = smtp.return_value.__enter__.return_value
        connection.login.assert_called_once_with(
            "sender@example.com", "test-only-password"
        )
        message = connection.send_message.call_args.args[0]
        self.assertEqual(message["To"], "recipient@example.com")
        self.assertEqual(message["From"], "sender@example.com")
        self.assertIn("successfully", message["Subject"])
        self.assertIn(os.environ["GITHUB_WORKFLOW_RUN_URL"],
                      message.get_body(preferencelist=("plain",)).get_content())
        self.assertIn("example/repo",
                      message.get_body(preferencelist=("html",)).get_content())

    @patch("scripts.calculate_scores.smtplib.SMTP_SSL")
    def test_custom_smtp_settings_and_delivery_failure(self, smtp):
        os.environ.update({
            "EMAIL_TO": "recipient@example.com",
            "SMTP_USERNAME": "sender@example.com",
            "SMTP_PASSWORD": "test-only-password",
            "SMTP_SERVER": "smtp.example.com",
            "SMTP_PORT": "2465"
        })
        connection = smtp.return_value.__enter__.return_value
        connection.send_message.side_effect = smtplib.SMTPException("delivery failed")
        with self.assertRaises(smtplib.SMTPException):
            calculate_scores.send_success_email({})
        self.assertEqual(smtp.call_args.args, ("smtp.example.com", 2465))


class SuccessEmailFlowTests(unittest.TestCase):
    def run_flow(self, publish_digest, scoring_error=None, digest_error=None):
        calls = Mock()
        calls.project.return_value = ("project", "score", "repo")
        calls.fetch.return_value = []
        calls.clear.side_effect = scoring_error
        calls.digest.side_effect = digest_error
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
        calls.email.assert_called_once_with({"example/repo": []})

    def test_manual_success_also_sends_email(self):
        calls = self.run_flow("false")
        calls.digest.assert_not_called()
        calls.email.assert_called_once_with({"example/repo": []})

    def test_scoring_failure_does_not_send_email(self):
        calls = self.run_flow("true", scoring_error=RuntimeError("scoring failed"))
        calls.email.assert_not_called()
        calls.digest.assert_not_called()

    def test_digest_failure_does_not_send_email(self):
        calls = self.run_flow("true", digest_error=RuntimeError("digest failed"))
        calls.email.assert_not_called()


if __name__ == "__main__":
    unittest.main()
