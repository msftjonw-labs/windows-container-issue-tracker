import json
import os
import tempfile
import unittest
from unittest.mock import patch

from scripts import send_weekly_email


class SendWeeklyEmailTests(unittest.TestCase):
    @patch("scripts.send_weekly_email.ssl.create_default_context")
    @patch("scripts.send_weekly_email.smtplib.SMTP")
    @patch.dict("os.environ", {
        "SMTP_HOST": "smtp.gmail.com",
        "SMTP_PORT": "",
        "SMTP_SECURITY": "starttls",
        "SMTP_USERNAME": "sender@example.com",
        "SMTP_PASSWORD": "app-password",
        "SMTP_FROM": "digest@example.com",
        "SMTP_TO": "one@example.com,two@example.com"
    }, clear=True)
    def test_sends_html_digest_over_starttls_by_default(self, smtp_client, context):
        smtp = smtp_client.return_value.__enter__.return_value
        smtp.send_message.return_value = {}

        send_weekly_email.send_weekly_email("<p>Digest</p>")

        smtp_client.assert_called_once_with(
            "smtp.gmail.com",
            587,
            timeout=30
        )
        smtp.starttls.assert_called_once_with(context=context.return_value)
        smtp.login.assert_called_once_with(
            "sender@example.com",
            "app-password"
        )
        message = smtp.send_message.call_args.args[0]
        self.assertEqual(message["From"], "digest@example.com")
        self.assertEqual(message["Subject"], "Windows Container Issue Tracker: sync succeeded")
        self.assertEqual(
            message["To"],
            "one@example.com, two@example.com"
        )
        self.assertEqual(
            smtp.send_message.call_args.kwargs["to_addrs"],
            ["one@example.com", "two@example.com"]
        )
        self.assertIn(
            "<p>Digest</p>",
            message.get_body(preferencelist=("html",)).get_content()
        )

    @patch.dict("os.environ", {
        "SMTP_HOST": "smtp.example.com",
        "SMTP_PORT": "465",
        "SMTP_SECURITY": "ssl",
        "SMTP_USERNAME": "sender@example.com",
        "SMTP_PASSWORD": "password",
        "SMTP_FROM_ADDRESS": "digest@example.com",
        "WEEKLY_EMAIL_RECIPIENTS": "recipient@example.com"
    }, clear=True)
    @patch("scripts.send_weekly_email.ssl.create_default_context")
    @patch("scripts.send_weekly_email.smtplib.SMTP_SSL")
    def test_supports_implicit_tls_and_legacy_settings(self, smtp_ssl, context):
        smtp = smtp_ssl.return_value.__enter__.return_value
        smtp.send_message.return_value = {}

        send_weekly_email.send_weekly_email("<p>Digest</p>")

        smtp_ssl.assert_called_once_with(
            "smtp.example.com",
            465,
            timeout=30,
            context=context.return_value
        )
        smtp.starttls.assert_not_called()
        smtp.send_message.assert_called_once()

    @patch.dict("os.environ", {
        "SMTP_HOST": "smtp.example.com",
        "SMTP_PORT": "",
        "SMTP_SECURITY": "starttls",
        "SMTP_FROM": "digest@example.com",
        "SMTP_TO": "recipient@example.com"
    }, clear=True)
    @patch("scripts.send_weekly_email.ssl.create_default_context")
    @patch("scripts.send_weekly_email.smtplib.SMTP")
    def test_allows_unauthenticated_smtp_relay(self, smtp_client, context):
        smtp = smtp_client.return_value.__enter__.return_value
        smtp.send_message.return_value = {}

        send_weekly_email.send_weekly_email("<p>Digest</p>")

        smtp_client.assert_called_once_with(
            "smtp.example.com",
            587,
            timeout=30
        )
        smtp.starttls.assert_called_once_with(context=context.return_value)
        smtp.login.assert_not_called()
        smtp.send_message.assert_called_once()

    @patch.dict("os.environ", {
        "SMTP_HOST": "smtp.example.com",
        "SMTP_SECURITY": "none",
        "SMTP_FROM": "digest@example.com",
        "SMTP_TO": "recipient@example.com"
    }, clear=True)
    def test_rejects_unencrypted_smtp(self):
        with self.assertRaisesRegex(
            ValueError,
            "SMTP_SECURITY must be starttls or ssl"
        ):
            send_weekly_email.send_weekly_email("<p>Digest</p>")

    @patch.dict("os.environ", {
        "SMTP_HOST": "digest@example.com",
        "SMTP_FROM": "digest@example.com",
        "SMTP_TO": "recipient@example.com"
    }, clear=True)
    def test_rejects_email_address_as_smtp_host(self):
        with self.assertRaisesRegex(ValueError, "SMTP_HOST must be an SMTP server hostname"):
            send_weekly_email.send_weekly_email("<p>Digest</p>")

    @patch.dict("os.environ", {
        "SMTP_HOST": "smtp.example.com",
        "SMTP_FROM": "digest@example.com",
        "SMTP_TO": "recipient@example.com",
        "SMTP_PORT": "65536"
    }, clear=True)
    def test_rejects_invalid_port(self):
        with self.assertRaisesRegex(ValueError, "SMTP_PORT must be between 1 and 65535"):
            send_weekly_email.send_weekly_email("<p>Digest</p>")

    @patch.dict("os.environ", {
        "SMTP_HOST": "smtp.example.com",
        "SMTP_FROM": "digest@example.com",
        "SMTP_TO": "recipient@example.com",
        "SMTP_USERNAME": "sender@example.com"
    }, clear=True)
    def test_requires_username_and_password_together(self):
        with self.assertRaisesRegex(ValueError, "SMTP_USERNAME and SMTP_PASSWORD must be set together"):
            send_weekly_email.send_weekly_email("<p>Digest</p>")

    @patch.dict("os.environ", {
        "SMTP_HOST": "smtp.example.com",
        "SMTP_FROM": "digest@example.com",
        "SMTP_TO": "recipient@example.com"
    }, clear=True)
    @patch("scripts.send_weekly_email.ssl.create_default_context")
    @patch("scripts.send_weekly_email.smtplib.SMTP")
    def test_fails_if_smtp_refuses_recipients(self, smtp_client, _context):
        smtp = smtp_client.return_value.__enter__.return_value
        smtp.send_message.return_value = {"recipient@example.com": (550, b"Rejected")}

        with self.assertRaisesRegex(RuntimeError, "refused one or more recipients"):
            send_weekly_email.send_weekly_email("<p>Digest</p>")

    @patch.dict("os.environ", {}, clear=True)
    def test_skips_if_smtp_host_is_unconfigured(self):
        send_weekly_email.send_weekly_email("<p>Digest</p>")

    def test_main_reads_html_from_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            payload_path = os.path.join(directory, "payload.json")
            with open(payload_path, "w", encoding="utf-8") as payload_file:
                json.dump({"html": "<p>Digest</p>"}, payload_file)

            with (
                patch("scripts.send_weekly_email.sys.argv", [
                    "send_weekly_email.py",
                    payload_path
                ]),
                patch(
                    "scripts.send_weekly_email.send_weekly_email"
                ) as send
            ):
                send_weekly_email.main()

        send.assert_called_once_with("<p>Digest</p>")

    def test_main_rejects_payload_without_html(self):
        with tempfile.TemporaryDirectory() as directory:
            payload_path = os.path.join(directory, "payload.json")
            with open(payload_path, "w", encoding="utf-8") as payload_file:
                json.dump({}, payload_file)

            with (
                patch("scripts.send_weekly_email.sys.argv", [
                    "send_weekly_email.py",
                    payload_path
                ]),
                self.assertRaisesRegex(
                    ValueError,
                    "Email payload must contain an HTML string"
                )
            ):
                send_weekly_email.main()


if __name__ == "__main__":
    unittest.main()
