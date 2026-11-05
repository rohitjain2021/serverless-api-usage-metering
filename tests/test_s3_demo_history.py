"""Validate synthetic history before it is used for customer/date-range reporting.

Inputs: local metadata standing in for the three deployed demo customer keys.
Outputs: assertions on full coverage, identities, hashes, quotas, and expected totals.
No AWS calls or S3 writes are performed by these tests.
"""

# Independently sum generated records and validate manifest hashes.
import hashlib
import unittest

# Import only pure dataset generation functions; the CLI is not executed on import.
from scripts.seed_s3_demo_history import build_dataset, json_bytes


class SyntheticHistoryTests(unittest.TestCase):
    """Verify complete, reproducible daily data and its billing-test boundaries."""

    def setUp(self):
        """Input: none beyond self. Build a local three-customer source; return None."""
        self.source = {"bucket": "local-test-bucket", "aws_account_id": "123456789012",
                       "aws_region": "us-east-1", "usage_plan_id": "shared01",
                       "usage_plan_name": "API Usage - Monthly Metering", "quota_limit": 10000,
                       "quota_period": "MONTH", "customers": [
                           {"customer_id": "cust-001", "customer_name": "Acme Ltd", "api_key_id": "acme001",
                            "api_key_name": "acme-dev-api-key", "base_requests": 20},
                           {"customer_id": "cust-002", "customer_name": "Northwind Ltd", "api_key_id": "north001",
                            "api_key_name": "northwind-dev-api-key", "base_requests": 40},
                           {"customer_id": "cust-003", "customer_name": "Contoso Ltd", "api_key_id": "contoso001",
                            "api_key_name": "contoso-dev-api-key", "base_requests": 60},
                       ]}

    def test_coverage_totals_and_hashes(self):
        """Input: three customers. Expect 92 days, 276 unique records, and matching manifest totals."""
        documents, manifest = build_dataset(self.source)
        self.assertEqual(len(documents), 92)
        self.assertEqual(manifest["record_count"], 276)
        self.assertEqual(manifest["start_date"], "2026-08-01")
        self.assertEqual(manifest["end_date"], "2026-10-31")
        records = [row for document in documents.values() for row in document["records"]]
        self.assertEqual(len({row["record_id"] for row in records}), 276)
        self.assertEqual(sum(row["requests_used"] for row in records), manifest["expected_total_requests"])
        self.assertTrue(all(row["is_synthetic"] and row["billing_status"] == "not_calculated" for row in records))
        for customer in self.source["customers"]:
            expected = sum(row["requests_used"] for row in records if row["customer_id"] == customer["customer_id"])
            self.assertEqual(expected, manifest["expected_customer_totals"][customer["customer_id"]])
        for entry in manifest["files"]:
            document = documents[f"date={entry['usage_date']}/usage.json"]
            self.assertEqual(hashlib.sha256(json_bytes(document)).hexdigest(), entry["sha256"])
            self.assertEqual(document["record_count"], 3)
        self.assertEqual(build_dataset(self.source), (documents, manifest))

    def test_zero_days_spike_and_monthly_quota_reset(self):
        """Inputs: selected boundary/scenario dates. Expect explicit zeros, a 300-request spike, and reset quota."""
        documents, _ = build_dataset(self.source)
        first_day = {row["customer_id"]: row for row in documents["date=2026-08-01/usage.json"]["records"]}
        self.assertEqual([first_day[key]["requests_used"] for key in ("cust-001", "cust-002", "cust-003")], [10, 20, 30])
        zero_day = next(row for row in documents["date=2026-08-07/usage.json"]["records"] if row["customer_id"] == "cust-002")
        self.assertEqual(zero_day["requests_used"], 0)
        self.assertEqual(zero_day["test_scenario"], "zero_usage")
        spike = next(row for row in documents["date=2026-09-15/usage.json"]["records"] if row["customer_id"] == "cust-003")
        self.assertEqual(spike["requests_used"], 300)
        september_start = next(row for row in documents["date=2026-09-01/usage.json"]["records"] if row["customer_id"] == "cust-001")
        self.assertEqual(september_start["quota_remaining"], 10000 - september_start["requests_used"])


# Allow the same tests to run through unittest discovery or direct execution.
if __name__ == "__main__":
    unittest.main()
