"""Test manifest-verified S3 queries and the UI source connection without network calls.

Inputs: generated synthetic documents served by a small in-memory S3 test double.
Outputs: assertions on cross-month totals, zero days, coverage errors, and Streamlit controls.
"""

# Build isolated JSON/body streams and manipulate test selections.
from datetime import date
import hashlib
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from botocore.exceptions import NoCredentialsError

# Exercise the same reader and Streamlit page used in the deployed frontend.
from streamlit.testing.v1 import AppTest
from frontend.s3_source import S3HistorySource
from scripts.seed_s3_demo_history import build_dataset, json_bytes, S3_PREFIX

# Source the customer identities from the one existing local fixture.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
UI_PATH = PROJECT_ROOT / "frontend" / "app.py"


class MemoryS3:
    """Serve exact byte objects with a fresh stream per read; no AWS operations occur."""

    def __init__(self, objects):
        """Input: S3 key/bytes mapping. Copy it for mutation tests; return None."""
        self.objects = dict(objects)

    def get_object(self, *, Bucket, Key):
        """Inputs: bucket and object key. Return a body stream or fail on a missing object."""
        return {"Body": io.BytesIO(self.objects[Key])}


class S3SourceTests(unittest.TestCase):
    """Verify reporting and UI behavior against complete and intentionally invalid datasets."""

    def setUp(self):
        """Input: no arguments beyond self. Create the in-memory three-month dataset."""
        fixture = json.loads((PROJECT_ROOT / "samples" / "api-gateway-usage.json").read_text())
        customers = [{"customer_id": key["tags"]["customer_id"], "customer_name": key["tags"]["customer_name"],
                      "api_key_id": key["id"], "api_key_name": key["name"], "base_requests": base}
                     for key, base in zip(fixture["get_api_keys_response"]["items"], (20, 40, 60))]
        source = {"bucket": "test-history-bucket", "aws_account_id": "123456789012", "aws_region": "us-east-1",
                  "usage_plan_id": "shared01", "usage_plan_name": "API Usage - Monthly Metering",
                  "quota_limit": 10000, "quota_period": "MONTH", "customers": customers}
        self.documents, self.manifest = build_dataset(source)
        objects = {f"{S3_PREFIX}/{key}": json_bytes(document) for key, document in self.documents.items()}
        objects[f"{S3_PREFIX}/manifest.json"] = json_bytes(self.manifest)
        self.s3 = MemoryS3(objects)

    def reader(self):
        """Input: current in-memory objects. Return a newly validated reader."""
        return S3HistorySource(self.s3, "test-history-bucket", S3_PREFIX)

    def test_three_month_and_cross_month_totals(self):
        """Input: Acme's full history and Aug 31–Sep 1. Expect 2,482 and 54 requests."""
        reader = self.reader()
        self.assertEqual(reader.available_source()["start_date"], date(2026, 8, 1))
        report = reader.generate_report("cust-001", date(2026, 8, 1), date(2026, 10, 31))
        self.assertEqual(report["total_requests"], 2482)
        self.assertEqual(len(report["source_objects"]), 92)
        cross_month = reader.generate_report("cust-001", date(2026, 8, 31), date(2026, 9, 1))
        self.assertEqual(cross_month["total_requests"], 54)
        self.assertEqual(cross_month["days_selected"], 2)
        self.assertIsNone(cross_month["source_month"])

    def test_explicit_zero_usage_is_valid(self):
        """Input: Northwind's zero-usage Aug 7. Expect zero requests with a verified source file."""
        report = self.reader().generate_report("cust-002", date(2026, 8, 7), date(2026, 8, 7))
        self.assertEqual(report["total_requests"], 0)
        self.assertEqual(len(report["source_objects"]), 1)
        self.assertEqual(report["usage"][0]["daily_usage"][0]["requests_used"], 0)

    def test_missing_date_is_not_zero(self):
        """Input: manifest without one date. Expect rejection before calculating a report."""
        self.manifest["files"].pop()
        self.s3.objects[f"{S3_PREFIX}/manifest.json"] = json_bytes(self.manifest)
        with self.assertRaisesRegex(ValueError, "coverage"):
            self.reader()

    def test_tampered_file_is_rejected(self):
        """Input: a changed day without a matching published hash. Expect a hash error."""
        self.s3.objects[f"{S3_PREFIX}/date=2026-08-01/usage.json"] = b'{}'
        with self.assertRaisesRegex(ValueError, "hash"):
            self.reader().generate_report("cust-001", date(2026, 8, 1), date(2026, 8, 1))

    def test_missing_customer_is_not_zero(self):
        """Input: a consistent-hash daily file missing a customer. Expect a coverage error."""
        document = self.documents["date=2026-08-01/usage.json"]
        document["records"].pop()
        document["record_count"] = 2
        document["total_requests"] = sum(row["requests_used"] for row in document["records"])
        payload = json_bytes(document)
        entry = self.manifest["files"][0]
        entry.update(sha256=hashlib.sha256(payload).hexdigest(), record_count=2)
        self.s3.objects[entry["key"]] = payload
        self.s3.objects[f"{S3_PREFIX}/manifest.json"] = json_bytes(self.manifest)
        with self.assertRaisesRegex(ValueError, "missing customer"):
            self.reader().generate_report("cust-001", date(2026, 8, 1), date(2026, 8, 1))

    def test_ui_generates_s3_report(self):
        """Input: configured S3 mode and cross-month calendar dates. Expect 54 requests in the UI."""
        with patch.dict(os.environ, {"USAGE_DATA_SOURCE": "s3", "USAGE_S3_BUCKET": "test-history-bucket",
                                     "USAGE_S3_PREFIX": S3_PREFIX}), patch("frontend.connection.s3_client", return_value=self.s3):
            page = AppTest.from_file(str(UI_PATH), default_timeout=30).run()
            self.assertFalse(page.exception)
            self.assertIn("S3 synthetic history", page.info[0].value)
            page.date_input[0].set_value(date(2026, 8, 31))
            page.date_input[1].set_value(date(2026, 9, 1))
            page.button[0].click().run()
            self.assertFalse(page.exception)
            self.assertEqual(page.metric[0].value, "54")
            self.assertEqual(len(page.get("download_button")), 2)

    def test_s3_connection_error_does_not_fall_back_to_sample(self):
        """Input: missing credentials in S3 mode. Expect a visible error and no sample totals."""
        with patch.dict(os.environ, {"USAGE_DATA_SOURCE": "s3", "USAGE_S3_BUCKET": "test-history-bucket",
                                     "USAGE_S3_PREFIX": S3_PREFIX}), patch(
                "frontend.connection.s3_client", side_effect=NoCredentialsError()):
            page = AppTest.from_file(str(UI_PATH), default_timeout=30).run()
            self.assertFalse(page.exception)
            self.assertIn("Unable to load usage data", page.error[0].value)
            self.assertFalse(page.metric)
            self.assertFalse(page.date_input)

    def test_source_change_clears_previous_report(self):
        """Input: local report then S3 source selection. Expect reset filters and no stale local report."""
        with patch.dict(os.environ, {"USAGE_DATA_SOURCE": "local", "USAGE_S3_BUCKET": "test-history-bucket",
                                     "USAGE_S3_PREFIX": S3_PREFIX}), patch("frontend.connection.s3_client", return_value=self.s3):
            page = AppTest.from_file(str(UI_PATH), default_timeout=30).run()
            page.button[0].click().run()
            self.assertEqual(page.metric[0].value, "310")
            page.radio[0].set_value("S3 demo history").run()
            self.assertFalse(page.exception)
            self.assertFalse(page.metric)
            self.assertEqual(page.date_input[0].value, date(2026, 8, 1))
            page.button[0].click().run()
            self.assertEqual(page.metric[0].value, "2,482")


# Reuse the frontend's existing unittest discovery command.
if __name__ == "__main__":
    unittest.main()
