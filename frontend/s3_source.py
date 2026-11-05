"""Read the versioned synthetic history dataset from S3 and calculate customer reports.

Inputs: an authenticated S3 client, bucket/prefix, customer ID, and inclusive dates.
Outputs: source choices and exact customer totals with source object/hash evidence.
Only GetObject is used; this reader never writes objects or changes native usage.
"""

# Validate canonical dates, content hashes, and JSON payloads.
from datetime import date, timedelta
import hashlib
import json
import re
# Fetch independent daily documents concurrently through one thread-safe SDK client.
from concurrent.futures import ThreadPoolExecutor

# Bound document size and parallel reads for this small demo dataset.
MAX_DOCUMENT_BYTES = 5 * 1024 * 1024
MAX_READ_WORKERS = 8
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


class S3HistorySource:
    """Read one complete manifest and expose customer/date queries over its daily files."""

    def __init__(self, client, bucket, prefix):
        """Input: S3 SDK client and dataset location. Validate/load manifest; return None.

        Raise ValueError for missing settings, incomplete/unsafe manifests, or invalid dates.
        AWS credential and object-access errors propagate to the caller.
        """
        if not bucket or not prefix:
            raise ValueError("Configure the S3 history bucket and prefix before connecting")
        self.client = client
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.manifest = self._read_json(self.prefix + "/manifest.json")
        manifest = self.manifest
        # Older demo manifests omit region; use the configured SDK region in that case.
        self.region = manifest.get("aws_region") or getattr(getattr(client, "meta", None), "region_name", "")
        if (manifest.get("schema_version") != 1 or manifest.get("status") != "complete"
                or manifest.get("is_synthetic") is not True or manifest.get("source_type") != "synthetic_demo"
                or manifest.get("prefix") != self.prefix or manifest.get("bucket") != self.bucket
                or manifest.get("timezone") != "UTC"):
            raise ValueError("Expected a complete, explicitly synthetic UTC demo dataset")
        self.start_date = date.fromisoformat(manifest["start_date"])
        self.end_date = date.fromisoformat(manifest["end_date"])
        if self.end_date < self.start_date or (self.end_date - self.start_date).days > 365:
            raise ValueError("Dataset dates must cover an ordered period of at most one year")
        self.customers = {}
        for customer in manifest["customers"]:
            identifier = customer["customer_id"]
            name = customer["customer_name"]
            if not identifier or identifier in self.customers:
                raise ValueError("Manifest contains empty or duplicate customer IDs")
            self.customers[identifier] = name
        self.files = {}
        for entry in manifest["files"]:
            day = date.fromisoformat(entry["usage_date"])
            if (day in self.files or entry["key"] != f"{self.prefix}/date={day.isoformat()}/usage.json"
                    or not SHA256_PATTERN.fullmatch(entry["sha256"])):
                raise ValueError("Manifest contains duplicate dates, unsafe keys, or invalid hashes")
            self.files[day] = entry
        expected_days = {self.start_date + timedelta(days=index)
                         for index in range((self.end_date - self.start_date).days + 1)}
        if not self.customers or set(self.files) != expected_days or manifest["daily_file_count"] != len(self.files):
            raise ValueError("Dataset coverage is incomplete; missing dates cannot be treated as zero")

    def _read_json(self, key, expected_hash=None):
        """Input: exact manifest-authorized S3 key and optional SHA-256. Return parsed JSON.

        Enforce a size bound and reject changed content. Missing/denied objects propagate;
        they are never silently replaced with sample or zero-usage records.
        """
        response = self.client.get_object(Bucket=self.bucket, Key=key)
        try:
            data = response["Body"].read(MAX_DOCUMENT_BYTES + 1)
        finally:
            response["Body"].close()
        if len(data) > MAX_DOCUMENT_BYTES:
            raise ValueError("Usage document exceeds the supported size")
        if expected_hash and hashlib.sha256(data).hexdigest() != expected_hash:
            raise ValueError("Usage file hash differs from its published manifest")
        return json.loads(data)

    def available_source(self):
        """Input: none beyond self. Return UI customer choices, available dates, and source label."""
        return {"customers": dict(sorted(self.customers.items(), key=lambda item: (item[1], item[0]))),
                "start_date": self.start_date, "end_date": self.end_date, "region": self.region,
                "label": "S3 synthetic history", "dataset_id": self.manifest["dataset_id"]}

    def _daily_document(self, day):
        """Input: selected date. Return its checked document; reject corrupt/incomplete records."""
        entry = self.files[day]
        document = self._read_json(entry["key"], entry["sha256"])
        records = document["records"]
        if (document.get("dataset_id") != self.manifest["dataset_id"] or document.get("is_synthetic") is not True
                or document.get("source_type") != "synthetic_demo" or document.get("usage_date") != day.isoformat()
                or document.get("timezone") != "UTC" or len(records) != entry["record_count"]
                or document.get("record_count") != len(records)):
            raise ValueError("Daily document identity, coverage, or record count is invalid")
        seen = set()
        for row in records:
            identity = (row["customer_id"], row["usage_plan_id"], row["api_key_id"])
            if (identity in seen or row["customer_id"] not in self.customers
                    or row.get("usage_date") != day.isoformat() or row.get("is_synthetic") is not True
                    or row.get("source_type") != "synthetic_demo" or type(row["requests_used"]) is not int
                    or row["requests_used"] < 0):
                raise ValueError("Invalid or duplicate customer/key usage record")
            seen.add(identity)
        if {row["customer_id"] for row in records} != set(self.customers):
            raise ValueError("Daily file is missing customer records; zero must be explicit")
        if sum(row["requests_used"] for row in records) != document["total_requests"]:
            raise ValueError("Daily request total does not match its records")
        return document

    def generate_report(self, customer_id, start_date, end_date):
        """Calculate an inclusive customer/date report from verified S3 daily records.

        Inputs: known customer ID and dates entirely inside manifest coverage.
        Returns: a report compatible with existing UI/downloads, including object
        keys and hashes for traceability. Cross-month ranges and zero days are supported.
        Raise on invalid selections or any missing/tampered source data.
        """
        if customer_id not in self.customers:
            raise ValueError("Select a customer present in the S3 dataset")
        if not isinstance(start_date, date) or not isinstance(end_date, date):
            raise ValueError("Select both a start date and an end date")
        if start_date > end_date or start_date < self.start_date or end_date > self.end_date:
            raise ValueError("Select ordered dates within the available S3 history")
        days = [start_date + timedelta(days=index) for index in range((end_date - start_date).days + 1)]
        with ThreadPoolExecutor(max_workers=MAX_READ_WORKERS) as pool:
            documents = list(pool.map(self._daily_document, days))
        usage = {}
        month = start_date.strftime("%Y-%m") if start_date.strftime("%Y-%m") == end_date.strftime("%Y-%m") else ""
        for document in documents:
            for record in document["records"]:
                if record["customer_id"] != customer_id:
                    continue
                identity = (record["usage_plan_id"], record["api_key_id"])
                if identity not in usage:
                    usage[identity] = {"month": month, "start_date": start_date.isoformat(), "end_date": end_date.isoformat(),
                                       "customer_id": customer_id, "customer_name": self.customers[customer_id],
                                       "customer_mapping_status": "mapped", "usage_plan_id": record["usage_plan_id"],
                                       "usage_plan_name": record["usage_plan_name"], "api_key_id": record["api_key_id"],
                                       "api_key_name": record["api_key_name"], "requests_used": 0, "daily_usage": []}
                usage[identity]["requests_used"] += record["requests_used"]
                usage[identity]["daily_usage"].append({"date": document["usage_date"], "requests_used": record["requests_used"],
                                                        "quota_remaining": record["quota_remaining"], "record_id": record["record_id"]})
        rows = [usage[key] for key in sorted(usage)]
        return {"schema_version": 2, "report_type": "customer_date_range", "source": "s3_synthetic_history",
                "is_synthetic": True, "dataset_id": self.manifest["dataset_id"], "bucket": self.bucket,
                "prefix": self.prefix, "source_month": month or None, "region": self.region,
                "customer_id": customer_id, "customer_name": self.customers[customer_id],
                "start_date": start_date.isoformat(), "end_date": end_date.isoformat(), "days_selected": len(days),
                "total_requests": sum(row["requests_used"] for row in rows),
                "api_key_count": len({row["api_key_id"] for row in rows}),
                "usage_plan_count": len({row["usage_plan_id"] for row in rows}), "usage": rows,
                "source_objects": [{"key": self.files[day]["key"], "sha256": self.files[day]["sha256"]} for day in days]}
