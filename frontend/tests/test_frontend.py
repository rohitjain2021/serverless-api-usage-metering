"""Verify customer/date filtering and rendered Streamlit form interactions locally.

Inputs: the existing sample plus selected customer/date values.
Outputs: assertions on totals, downloadable contents, and visible UI validation.
"""

# Read the two download formats, create date selections, and run standard unittest cases.
import csv
from datetime import date
import io
import json
from pathlib import Path
import unittest
# Supply isolated extra-key data without adding another committed sample fixture.
import tempfile

# Use Streamlit's own simulated browser/session interface for actual UI interactions.
from streamlit.testing.v1 import AppTest
from frontend.report_service import available_source, downloads, generate_customer_report
from scripts.run_sample_report import DEFAULT_SAMPLE

# Locate the UI independently of the directory from which tests are launched.
UI_PATH = Path(__file__).resolve().parents[1] / "app.py"


class ReportServiceTests(unittest.TestCase):
    """Check calculations and downloaded values at the UI service boundary."""

    def test_selected_customer_and_inclusive_range(self):
        """Input: Northwind, October 5–10. Expect 6 days, 120 requests, and matching downloads."""
        report = generate_customer_report("cust-002", date(2026, 10, 5), date(2026, 10, 10))
        self.assertEqual(report["total_requests"], 120)
        self.assertEqual(report["days_selected"], 6)
        self.assertEqual(report["customer_name"], "Northwind Ltd")
        self.assertEqual(len(report["usage"][0]["daily_usage"]), 6)
        csv_bytes, json_bytes = downloads(report)
        self.assertEqual(json.loads(json_bytes)["total_requests"], 120)
        rows = list(csv.DictReader(io.StringIO(csv_bytes.decode())))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["start_date"], "2026-10-05")
        self.assertEqual(rows[0]["end_date"], "2026-10-10")
        self.assertEqual(rows[0]["requests_used"], "120")

    def test_one_day(self):
        """Input: Contoso on October 31 only. Expect 30 requests and one day."""
        report = generate_customer_report("cust-003", date(2026, 10, 31), date(2026, 10, 31))
        self.assertEqual(report["total_requests"], 30)
        self.assertEqual(report["days_selected"], 1)

    def test_invalid_customer_and_dates(self):
        """Inputs: missing customer, reversed dates, and absent data period. Expect ValueError."""
        for customer, start, end in [
            ("unknown", date(2026, 10, 1), date(2026, 10, 31)),
            ("cust-001", date(2026, 10, 20), date(2026, 10, 10)),
            ("cust-001", date(2026, 9, 30), date(2026, 10, 2)),
            ("cust-001", None, date(2026, 10, 2)),
        ]:
            with self.subTest(customer=customer, start=start), self.assertRaises(ValueError):
                generate_customer_report(customer, start, end)

    def test_customer_with_multiple_keys(self):
        """Input: Acme owns two sample keys. Expect one dropdown choice and 620 total requests."""
        sample = json.loads(DEFAULT_SAMPLE.read_text())
        extra_key = dict(sample["get_api_keys_response"]["items"][0],
                         id="acme002", name="acme-secondary-api-key")
        sample["get_api_keys_response"]["items"].append(extra_key)
        usage = sample["get_usage_responses_by_plan_id"]["shared01"]["items"]
        usage["acme002"] = usage["acme001"]
        with tempfile.TemporaryDirectory() as directory:
            sample_path = Path(directory) / "sample.json"
            sample_path.write_text(json.dumps(sample))
            self.assertEqual(len(available_source(sample_path)["customers"]), 3)
            report = generate_customer_report("cust-001", date(2026, 10, 1), date(2026, 10, 31), sample_path)
        self.assertEqual(report["total_requests"], 620)
        self.assertEqual(report["api_key_count"], 2)


class StreamlitTests(unittest.TestCase):
    """Exercise the real Streamlit page, not a separate mock form."""

    def test_generate_report_from_customer_dropdown(self):
        """Input: load page and submit its default Acme/month selection. Expect 310 requests."""
        page = AppTest.from_file(str(UI_PATH), default_timeout=30).run()
        self.assertFalse(page.exception)
        self.assertEqual(len(page.selectbox), 1)
        self.assertEqual(len(page.date_input), 2)
        self.assertFalse(page.metric)
        page.button[0].click().run()
        self.assertFalse(page.exception)
        self.assertEqual(page.metric[0].value, "310")
        self.assertEqual(len(page.get("download_button")), 2)

    def test_customer_and_dates_change_totals(self):
        """Input: select Northwind and October 5–10 in the UI. Expect 120 requests."""
        page = AppTest.from_file(str(UI_PATH), default_timeout=30).run()
        page.selectbox[0].set_value("cust-002")
        page.date_input[0].set_value(date(2026, 10, 5))
        page.date_input[1].set_value(date(2026, 10, 10))
        page.button[0].click().run()
        self.assertFalse(page.exception)
        self.assertEqual(page.metric[0].value, "120")

    def test_invalid_range_clears_previous_report(self):
        """Input: valid report then reversed dates. Expect visible error and no stale downloads."""
        page = AppTest.from_file(str(UI_PATH), default_timeout=30).run()
        page.button[0].click().run()
        page.date_input[0].set_value(date(2026, 10, 20))
        page.date_input[1].set_value(date(2026, 10, 10))
        page.button[0].click().run()
        self.assertFalse(page.exception)
        self.assertIn("Start date", page.error[0].value)
        self.assertFalse(page.metric)
        self.assertFalse(page.get("download_button"))


# Support direct execution alongside unittest discovery from the repository root.
if __name__ == "__main__":
    unittest.main()
