import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from scripts import notify_run_status


class NotifyRunStatusTests(unittest.TestCase):
    def setUp(self):
        self.snapshot_tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.snapshot_tempdir.cleanup)
        snapshot_directory = Path(self.snapshot_tempdir.name)
        (snapshot_directory / "windows-container-issue-snapshot-2026-10-23.pdf").write_bytes(
            b"%PDF test attachment"
        )
        (snapshot_directory / "windows-container-hypothesis-validation-2026-10-23.pdf").write_bytes(
            b"%PDF hypothesis attachment"
        )
        self.env = {
            "SMTP_HOST": "smtp.example.com",
            "SMTP_USERNAME": "sender",
            "SMTP_PASSWORD": "test-only-placeholder",
            "SMTP_FROM": "sender@example.com",
            "WEEKLY_EMAIL_RECIPIENTS": "recipient@example.com",
            "RUN_STATUS": "success",
            "RUN_URL": "https://github.com/example/repo/actions/runs/123",
            "GITHUB_REPOSITORY": "example/repo",
            "PROJECT_URL": "https://github.com/orgs/example/projects/1",
            "SNAPSHOT_DIRECTORY": str(snapshot_directory),
            "REPORT_DIRECTORY": str(snapshot_directory),
            "REPORT_STATUS": "success",
        }

    @patch("scripts.notify_run_status.smtplib.SMTP_SSL")
    def test_sends_status_and_run_link_over_authenticated_tls(self, smtp_ssl):
        for status in ("success", "failure", "cancelled"):
            with self.subTest(status=status):
                self.env["RUN_STATUS"] = status
                with patch.dict("os.environ", self.env, clear=True):
                    notify_run_status.main()

                args, kwargs = smtp_ssl.call_args
                self.assertEqual(args, ("smtp.example.com", 465))
                self.assertEqual(kwargs["timeout"], 30)
                self.assertTrue(kwargs["context"].check_hostname)
                smtp = smtp_ssl.return_value.__enter__.return_value
                smtp.login.assert_called_with("sender", "test-only-placeholder")
                message = smtp.send_message.call_args.args[0]
                self.assertEqual(message["To"], "recipient@example.com")
                self.assertEqual(message["From"], "sender@example.com")
                self.assertIn(status, message["Subject"])
                self.assertIn("example/repo", message["Subject"])
                body = message.get_body(preferencelist=("plain",)).get_content()
                self.assertIn(status, body)
                self.assertIn(self.env["RUN_URL"], body)
                self.assertIn(self.env["PROJECT_URL"], body)
                attachment = list(message.iter_attachments())[0]
                self.assertEqual(
                    attachment.get_filename(),
                    "windows-container-issue-snapshot-2026-10-23.pdf",
                )
                self.assertEqual(attachment.get_content_type(), "application/pdf")
                self.assertEqual(attachment.get_payload(decode=True), b"%PDF test attachment")
                attachments = list(message.iter_attachments())
                self.assertEqual(len(attachments), 2)
                self.assertEqual(
                    attachments[1].get_filename(),
                    "windows-container-hypothesis-validation-2026-10-23.pdf",
                )
                self.assertEqual(attachments[1].get_payload(decode=True), b"%PDF hypothesis attachment")
                self.assertIn("Hypothesis report status: success", body)

    @patch("scripts.notify_run_status.smtplib.SMTP_SSL")
    def test_supports_multiple_weekly_recipients(self, smtp_ssl):
        recipients = "first@example.com, second@example.com"
        self.env["WEEKLY_EMAIL_RECIPIENTS"] = recipients
        with patch.dict("os.environ", self.env, clear=True):
            notify_run_status.main()

        smtp = smtp_ssl.return_value.__enter__.return_value
        message = smtp.send_message.call_args.args[0]
        self.assertEqual(
            [address.addr_spec for address in message["To"].addresses],
            ["first@example.com", "second@example.com"],
        )

    @patch("scripts.notify_run_status.smtplib.SMTP_SSL")
    def test_missing_or_empty_configuration_fails_before_connecting(self, smtp_ssl):
        for name in self.env:
            for value in (None, ""):
                with self.subTest(name=name, value=value):
                    env = self.env.copy()
                    if value is None:
                        del env[name]
                    else:
                        env[name] = value
                    with patch.dict("os.environ", env, clear=True):
                        with self.assertRaisesRegex(ValueError, name):
                            notify_run_status.main()
        smtp_ssl.assert_not_called()

    @patch("scripts.notify_run_status.smtplib.SMTP_SSL")
    def test_delivery_failure_is_not_silenced(self, smtp_ssl):
        smtp = smtp_ssl.return_value.__enter__.return_value
        smtp.send_message.side_effect = RuntimeError("Delivery failed")
        with patch.dict("os.environ", self.env, clear=True):
            with self.assertRaisesRegex(RuntimeError, "Delivery failed"):
                notify_run_status.main()

    @patch("scripts.notify_run_status.smtplib.SMTP_SSL")
    def test_failed_scoring_still_emails_without_pdfs(self, smtp_ssl):
        self.env["SNAPSHOT_DIRECTORY"] = self.snapshot_tempdir.name + "/empty"
        self.env["REPORT_DIRECTORY"] = self.snapshot_tempdir.name + "/empty"
        self.env["RUN_STATUS"] = "failure"
        self.env["REPORT_STATUS"] = "skipped"
        with patch.dict("os.environ", self.env, clear=True):
            notify_run_status.main()
        smtp = smtp_ssl.return_value.__enter__.return_value
        message = smtp.send_message.call_args.args[0]
        self.assertEqual(list(message.iter_attachments()), [])
        body = message.get_content()
        self.assertIn("Issue scoring run status: failure", body)
        self.assertIn("Hypothesis report status: skipped", body)
        self.assertIn("Attachment unavailable: issue snapshot", body)
        self.assertIn("Attachment unavailable: hypothesis report", body)

    @patch("scripts.notify_run_status.smtplib.SMTP_SSL")
    def test_report_failure_preserves_snapshot_email(self, smtp_ssl):
        self.env["REPORT_STATUS"] = "failure"
        self.env["REPORT_DIRECTORY"] = self.snapshot_tempdir.name + "/empty"
        with patch.dict("os.environ", self.env, clear=True):
            notify_run_status.main()
        message = smtp_ssl.return_value.__enter__.return_value.send_message.call_args.args[0]
        self.assertEqual(len(list(message.iter_attachments())), 1)
        self.assertIn("Hypothesis report status: failure", message.get_body().get_content())


if __name__ == "__main__":
    unittest.main()
