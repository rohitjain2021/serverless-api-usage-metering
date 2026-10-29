"""Verify report behavior using sample responses; no AWS calls or uploads occur.

Run with ``python -m unittest discover -s tests -v``. Each test takes only the
unittest instance (self), returns None, and raises AssertionError on a mismatch.
"""

# Standard-library parsers inspect generated payloads; mocks replace AWS clients
# and environment settings so tests need neither AWS credentials nor network access.
import csv
import io
import json
import os
import unittest
from datetime import date, datetime, timezone
from unittest.mock import Mock, patch

from src import app


class PeriodTests(unittest.TestCase):
    """Check month selection with fixed reference dates and sample Lambda events."""

    def test_january_and_leap_year(self):
        """Inputs: January reference and February 2024. Expect December and leap day."""
        self.assertEqual(app.report_period({}, datetime(2026, 1, 1, tzinfo=timezone.utc)),
                         (date(2025, 12, 1), date(2025, 12, 31)))
        self.assertEqual(app.report_period({"month": "2024-02"}),
                         (date(2024, 2, 1), date(2024, 2, 29)))

    def test_delayed_schedule_uses_original_time(self):
        """Input: February 1 scheduled timestamp. Expect January despite execution delay."""
        self.assertEqual(app.report_period({"time": "2026-02-01T06:00:00Z"})[0], date(2026, 1, 1))

    def test_invalid_and_incomplete_months_fail(self):
        """Inputs: malformed/future/current months and naive time. Expect ValueError."""
        for month in ("2026-1", "2026-13", "nope", 202601, "9999-12"):
            with self.subTest(month=month), self.assertRaises(ValueError):
                app.report_period({"month": month})
        with self.assertRaises(ValueError):
            app.report_period({"month": "2026-10"}, datetime(2026, 10, 29, tzinfo=timezone.utc))
        with self.assertRaises(ValueError):
            app.report_period({"time": "2026-02-01T06:00:00"})


class ReportTests(unittest.TestCase):
    """Exercise collection, serialization, and handler uploads using sample clients."""

    def setUp(self):
        """Create fresh mock clients/data before each test; returns None.

        Inputs: none beyond self. Sample output state: plan p1, key k1, and
        September usage pairs [4, 96] and [6, 90], totaling 10 used requests.
        """
        self.api = Mock()
        self.api.meta.region_name = "us-east-1"
        self.api.get_usage_plans.return_value = {"items": [{"id": "p1", "name": "Standard"}]}
        self.api.get_api_keys.return_value = {"items": [{"id": "k1", "name": "Customer"}]}
        self.api.get_usage.return_value = {"items": {"k1": [[4, 96], [6, 90]]}}
        self.start, self.end = date(2026, 9, 1), date(2026, 9, 30)

    def collect(self, ids=()):
        """Input: optional plan IDs. Return a September report from the mock API."""
        return app.collect_report(self.api, self.start, self.end, ids)

    def test_pagination_for_plans_keys_and_usage(self):
        """Inputs: multiple API pages and a deleted key. Expect 12 requests and two rows."""
        # side_effect supplies successive response pages in the order they are called.
        self.api.get_usage_plans.side_effect = [
            {"items": [{"id": "p1"}], "position": "plans-next"}, {"items": [{"id": "p2"}]}]
        self.api.get_api_keys.side_effect = [
            {"items": [{"id": "k1", "name": "Customer"}], "position": "keys-next"}, {"items": []}]
        self.api.get_usage.side_effect = [
            {"items": {"k1": [[4, 96], [6, 90]]}, "position": "usage-next"},
            {"items": {"deleted-key": [[2, 8]]}}, {"items": {}}]
        report = self.collect()
        self.assertEqual(report["total_requests"], 12)
        self.assertEqual(report["usage_plan_count"], 2)
        self.assertEqual(report["api_key_plan_count"], 2)
        self.assertEqual(report["usage"][0]["api_key_name"], "")
        self.assertEqual(report["usage"][1]["daily_usage"][1]["date"], "2026-09-02")
        self.api.get_api_keys.assert_any_call(limit=500, includeValues=False, position="keys-next")
        self.api.get_usage.assert_any_call(limit=500, usagePlanId="p1", startDate="2026-09-01",
                                           endDate="2026-09-30", position="usage-next")

    def test_unknown_plan_fails(self):
        """Input: absent plan ID. Expect ValueError instead of an incomplete report."""
        with self.assertRaises(ValueError):
            self.collect({"missing"})

    def test_plan_filter(self):
        """Input: two plans but only p1 selected. Expect usage fetched for one plan."""
        self.api.get_usage_plans.return_value["items"].append({"id": "p2"})
        self.assertEqual(self.collect({"p1"})["usage_plan_count"], 1)
        self.api.get_usage.assert_called_once()

    def test_empty_report_has_csv_header(self):
        """Input: no plans. Expect zero requests and a CSV containing only its header."""
        self.api.get_usage_plans.return_value = {"items": []}
        report = self.collect()
        self.assertEqual(report["total_requests"], 0)
        self.assertEqual(list(csv.reader(io.StringIO(app.csv_report(report).decode()))), [list(app.FIELDS)])

    def test_csv_names_are_escaped_and_quoted(self):
        """Input: a formula-like key name. Expect escaped text and a total of 10."""
        self.api.get_api_keys.return_value["items"][0]["name"] = '=HYPERLINK("bad,link")'
        report = self.collect()
        row = next(csv.DictReader(io.StringIO(app.csv_report(report).decode())))
        self.assertEqual(row["api_key_name"], '\'=HYPERLINK("bad,link")')
        self.assertEqual(row["requests_used"], "10")

    def test_repeated_tokens_and_duplicate_keys_fail(self):
        """Inputs: looping pagination and duplicate keys. Expect RuntimeError for each."""
        self.api.get_usage.side_effect = [
            {"items": {}, "position": "same"}, {"items": {}, "position": "same"}]
        with self.assertRaises(RuntimeError):
            self.collect()
        self.api.get_usage.side_effect = [
            {"items": {"k1": [[1, 1]]}, "position": "next"}, {"items": {"k1": [[1, 1]]}}]
        with self.assertRaises(RuntimeError):
            self.collect()

    def test_handler_writes_encrypted_reports_and_retries_same_keys(self):
        """Input: the same September event twice. Expect two encrypted files per run.

        Assert stable destination keys, JSON total 10, and the configured AWS region.
        """
        s3 = Mock()
        # Patch settings and the client factory only within this block; no real S3
        # uploads occur. Recorded calls let us inspect the bytes sent to put_object.
        with patch.dict(os.environ, {"REPORT_BUCKET": "reports-bucket", "REPORT_PREFIX": "reports",
                                     "USAGE_PLAN_IDS": "p1"}), patch.object(app, "create_clients", return_value=(self.api, s3)):
            result = app.lambda_handler({"month": "2026-09"}, None)
            self.assertEqual(result, app.lambda_handler({"month": "2026-09"}, None))
        self.assertEqual(s3.put_object.call_count, 4)
        calls = s3.put_object.call_args_list
        self.assertEqual(calls[0].kwargs["Key"], calls[2].kwargs["Key"])
        payload = json.loads(calls[1].kwargs["Body"])
        self.assertEqual(payload["total_requests"], 10)
        self.assertEqual(payload["region"], "us-east-1")
        for call in calls:
            self.assertEqual(call.kwargs["ServerSideEncryption"], "AES256")

    def test_collection_failure_does_not_publish_partial_report(self):
        """Input: a simulated API failure. Expect it to propagate with no S3 writes."""
        self.api.get_usage.side_effect = RuntimeError("throttling exhausted")
        s3 = Mock()
        with patch.dict(os.environ, {"REPORT_BUCKET": "reports-bucket"}), patch.object(
                app, "create_clients", return_value=(self.api, s3)):
            with self.assertRaises(RuntimeError):
                app.lambda_handler({"month": "2026-09"}, None)
        s3.put_object.assert_not_called()

    def test_storage_failure_is_propagated(self):
        """Input: a simulated S3 denial. Expect the handler to fail rather than succeed."""
        s3 = Mock()
        s3.put_object.side_effect = RuntimeError("AccessDenied")
        with patch.dict(os.environ, {"REPORT_BUCKET": "reports-bucket"}), patch.object(
                app, "create_clients", return_value=(self.api, s3)), self.assertRaises(RuntimeError):
            app.lambda_handler({"month": "2026-09"}, None)


if __name__ == "__main__":
    # Allow direct execution as well as unittest discovery; failures set a nonzero exit.
    unittest.main()
