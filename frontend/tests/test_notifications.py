"""Verify report-download notifications without AWS calls or email delivery.

Inputs: generated reports, simulated Streamlit download clicks, and mocked SNS responses.
Outputs: assertions on event metadata, click-only publishing, and visible failure handling.
"""

# Keep standard-library, SDK, Streamlit, and application imports before test declarations.
from datetime import date
import json
import os
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from botocore.exceptions import ClientError
from streamlit.testing.v1 import AppTest

from frontend.notifications import publish_download_request
from frontend.report_service import generate_customer_report

UI_PATH = Path(__file__).resolve().parents[1] / "app.py"
TOPIC_ARN = "arn:aws:sns:us-east-1:123456789012:report-downloads"


class DownloadNotificationTests(unittest.TestCase):
    """Check notification contents and real widget callback behavior."""

    def test_publish_metadata_without_report_contents(self):
        """Input: Acme CSV download request. Expect one SNS event describing the displayed report."""
        report = generate_customer_report("cust-001", date(2026, 10, 1), date(2026, 10, 31))
        client = Mock()
        client.publish.return_value = {"MessageId": "accepted-id"}
        self.assertEqual(publish_download_request(report, "csv", TOPIC_ARN, client), "accepted-id")
        call = client.publish.call_args.kwargs
        event = json.loads(call["Message"])
        self.assertEqual(call["TopicArn"], TOPIC_ARN)
        self.assertEqual(event["total_requests"], 310)
        self.assertEqual(event["file_format"], "csv")
        self.assertEqual(event["requester_identity"], "anonymous")
        self.assertFalse(event["download_completion_verified"])
        self.assertNotIn("usage", event)
        self.assertIn("event_id", event)
        self.assertTrue(event["requested_at_utc"].endswith("+00:00"))

    def test_download_click_publishes_and_rerun_does_not(self):
        """Input: generate/rerun, then trigger each download widget. Expect one event per click only."""
        with patch("frontend.notifications.publish_download_request") as publish, patch(
                "frontend.connection.aws_client"), patch.dict(os.environ, {"USAGE_DOWNLOAD_TOPIC_ARN": TOPIC_ARN}):
            page = AppTest.from_file(str(UI_PATH), default_timeout=30).run()
            page.button[0].click().run()
            page.run()
            publish.assert_not_called()
            # AppTest exposes downloads as generic elements; send their normal button trigger state.
            for index, file_format in enumerate(("csv", "json")):
                states = page._tree.get_widget_states()
                states.widgets.add(id=page.get("download_button")[index].proto.id, trigger_value=True)
                page._run(states)
                self.assertFalse(page.exception)
                self.assertEqual(publish.call_args.args[1], file_format)
                self.assertEqual(publish.call_args.args[0]["total_requests"], 310)
                self.assertEqual(publish.call_count, index + 1)
                page.run()
                self.assertEqual(publish.call_count, index + 1)

    def test_notification_failure_keeps_downloads_available(self):
        """Input: SNS access denied on download click. Expect a safe warning and retained report/downloads."""
        error = ClientError({"Error": {"Code": "AccessDenied", "Message": "private-service-details"}}, "Publish")
        with patch("frontend.notifications.publish_download_request", side_effect=error), patch(
                "frontend.connection.aws_client"), patch.dict(os.environ, {"USAGE_DOWNLOAD_TOPIC_ARN": TOPIC_ARN}):
            page = AppTest.from_file(str(UI_PATH), default_timeout=30).run()
            page.button[0].click().run()
            states = page._tree.get_widget_states()
            states.widgets.add(id=page.get("download_button")[0].proto.id, trigger_value=True)
            page._run(states)
            self.assertFalse(page.exception)
            self.assertIn("notification could not be sent", page.warning[0].value)
            self.assertNotIn("private-service-details", page.warning[0].value)
            self.assertEqual(len(page.get("download_button")), 2)
            self.assertEqual(page.metric[0].value, "310")


# Support the frontend unittest discovery command used by CI.
if __name__ == "__main__":
    unittest.main()
