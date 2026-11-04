"""Exercise the real Lambda handler with the single local sample fixture.

Input: the committed October API Gateway sample and a temporary output directory.
Output: assertions against actual monthly CSV/JSON files; no AWS calls are made.
"""

# Read both output formats and isolate generated files in a disposable directory.
import csv
import json
from pathlib import Path
import tempfile
import unittest

# Reuse the same runner developers invoke manually.
from scripts.run_sample_report import run_sample


class SampleReportTests(unittest.TestCase):
    """Verify customer attribution and monthly arithmetic across the complete handler."""

    def test_sample_generates_monthly_customer_reports(self):
        """Input: one fixture. Expect two files, three mapped customers, and 1,860 requests.

        Returns None; assertion failures indicate incorrect totals, dates, or serialization.
        """
        with tempfile.TemporaryDirectory() as directory:
            result = run_sample(output_dir=directory)
            self.assertEqual(result["month"], "2026-10")
            self.assertEqual(result["total_requests"], 1860)
            self.assertEqual(len(result["local_files"]), 2)
            report_path = Path(directory) / "reports" / "2026-10" / "usage.json"
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["schema_version"], 2)
            self.assertEqual(report["usage_plan_count"], 1)
            totals = {row["customer_id"]: row["requests_used"] for row in report["usage"]}
            self.assertEqual(totals, {"cust-001": 310, "cust-002": 620, "cust-003": 930})
            # Confirm display names survive the ID-based join into the monthly report.
            acme = next(row for row in report["usage"] if row["customer_id"] == "cust-001")
            self.assertEqual(acme["customer_name"], "Acme Ltd")
            self.assertEqual(acme["api_key_name"], "acme-production-api-key")
            self.assertEqual(acme["usage_plan_name"], "API Usage - Monthly Metering")
            for row in report["usage"]:
                # All customers use the same service; their key tags identify the owner.
                self.assertEqual(row["usage_plan_id"], "shared01")
                self.assertEqual(row["usage_plan_name"], "API Usage - Monthly Metering")
                self.assertEqual(row["customer_mapping_status"], "mapped")
                self.assertTrue(row["customer_name"])
                self.assertEqual(len(row["daily_usage"]), 31)
                self.assertEqual(row["daily_usage"][0]["date"], "2026-10-01")
                self.assertEqual(row["daily_usage"][-1]["date"], "2026-10-31")
            with report_path.with_suffix(".csv").open(encoding="utf-8", newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual({row["customer_id"]: int(row["requests_used"]) for row in rows}, totals)
            self.assertEqual(len(list(Path(directory).rglob("*.csv"))), 1)


# Support direct invocation in addition to the existing unittest discovery command.
if __name__ == "__main__":
    unittest.main()
