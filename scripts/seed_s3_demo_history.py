"""Generate three synthetic usage months and optionally upload them to the demo S3 bucket.

Inputs: deployed demo stack metadata plus an explicit --upload flag for S3 writes.
Outputs: 92 daily JSON files and a manifest with totals/hashes under a versioned demo prefix.
Usage is invented for testing; no API requests are generated and no native counters are altered.
"""

# Parse CLI settings and build dates, JSON documents, checksums, and local paths.
import argparse
from datetime import date, timedelta
import hashlib
import json
from pathlib import Path
# Upload independent daily objects concurrently; publish the manifest only after verification.
from concurrent.futures import ThreadPoolExecutor

# Use the existing pinned AWS SDK and distinguish missing objects from permission failures.
import boto3
from botocore.exceptions import ClientError

# A fixed, completed three-month window makes this dataset reproducible across runs.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATASET_ID = "historical-2026-08_2026-10-v1"
START_DATE = date(2026, 8, 1)
END_DATE = date(2026, 10, 31)
S3_PREFIX = f"usage/demo/dataset={DATASET_ID}"
LOCAL_ROOT = PROJECT_ROOT / "output" / "synthetic-history" / DATASET_ID
DEFAULT_STACK = "serverless-api-usage-metering-dev"
DEFAULT_REGION = "us-east-1"
# Each source key must resolve to the expected customer before synthetic records are produced.
CUSTOMERS = (
    ("AcmeDemoApiKeyId", "cust-001", 20),
    ("NorthwindDemoApiKeyId", "cust-002", 40),
    ("ContosoDemoApiKeyId", "cust-003", 60),
)


def json_bytes(document):
    """Input: JSON-compatible object. Return canonical UTF-8 bytes for repeatable hashes."""
    return (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def load_demo_source(session, stack_name):
    """Read the demo bucket, plan, and customer identities without obtaining key values.

    Inputs: configured SDK session and demo stack name. Returns a metadata dictionary.
    Raises on incomplete deployment, missing/mismatched customer tags, or AWS errors.
    Historical usage will be synthetic even though these resource identifiers are real.
    """
    account_id = session.client("sts").get_caller_identity()["Account"]
    stack = session.client("cloudformation").describe_stacks(StackName=stack_name)["Stacks"][0]
    if stack["StackStatus"] not in ("CREATE_COMPLETE", "UPDATE_COMPLETE"):
        raise ValueError("The demo stack must be successfully deployed before seeding history")
    outputs = {entry["OutputKey"]: entry["OutputValue"] for entry in stack["Outputs"]}
    api = session.client("apigateway")
    plan = api.get_usage_plan(usagePlanId=outputs["DemoUsagePlanId"])
    customers = []
    for output_key, expected_id, base_requests in CUSTOMERS:
        key = api.get_api_key(apiKey=outputs[output_key], includeValue=False)
        tags = key.get("tags", {})
        if tags.get("customer_id") != expected_id or not tags.get("customer_name"):
            raise ValueError("Demo customer tags do not match the expected dataset identities")
        customers.append({"customer_id": expected_id, "customer_name": tags["customer_name"],
                          "api_key_id": key["id"], "api_key_name": key["name"],
                          "base_requests": base_requests})
    return {"aws_account_id": account_id, "aws_region": session.region_name,
            "bucket": outputs["ReportsBucketName"], "usage_plan_id": plan["id"],
            "usage_plan_name": plan["name"], "quota_limit": plan["quota"]["limit"],
            "quota_period": plan["quota"]["period"], "customers": customers}


def synthetic_requests(day, customer):
    """Input: date and customer metadata. Return (invented request count, test scenario).

    Weekends have lower usage, weekdays vary and grow by month. Explicit zero days
    and one September spike support date filtering and later explanation tests.
    These labels describe fixture construction, not real customer behavior.
    """
    month_index = (day.year - START_DATE.year) * 12 + day.month - START_DATE.month
    used = (customer["base_requests"] // 2 if day.weekday() >= 5 else
            customer["base_requests"] + month_index * 10 + (day.day % 5) * 2)
    scenario = "normal"
    if (customer["customer_id"] == "cust-002" and day.day in (7, 21)) or (
            customer["customer_id"] == "cust-001" and day == date(2026, 10, 10)):
        used, scenario = 0, "zero_usage"
    if customer["customer_id"] == "cust-003" and day == date(2026, 9, 15):
        used, scenario = 300, "usage_spike"
    return used, scenario


def build_dataset(source):
    """Build normalized daily usage documents and a completeness/verification manifest.

    Input: bucket/plan/customer metadata from load_demo_source or a local test double.
    Returns: (daily_documents, manifest), where documents map relative object keys to dicts.
    Includes every customer on every day, even zero usage. Quota snapshots reset monthly.
    No files or AWS resources are modified by this function.
    """
    documents = {}
    files = []
    monthly_totals = {}
    customer_totals = {customer["customer_id"]: 0 for customer in source["customers"]}
    quota_used = {}
    day = START_DATE
    while day <= END_DATE:
        month = day.strftime("%Y-%m")
        monthly_totals.setdefault(month, {customer_id: 0 for customer_id in customer_totals})
        rows = []
        for customer in source["customers"]:
            used, scenario = synthetic_requests(day, customer)
            quota_key = (month, customer["api_key_id"])
            quota_used[quota_key] = quota_used.get(quota_key, 0) + used
            rows.append({"record_id": f"{day.isoformat()}:{source['usage_plan_id']}:{customer['api_key_id']}",
                         "usage_date": day.isoformat(), "customer_id": customer["customer_id"],
                         "customer_name": customer["customer_name"], "api_key_id": customer["api_key_id"],
                         "api_key_name": customer["api_key_name"], "usage_plan_id": source["usage_plan_id"],
                         "usage_plan_name": source["usage_plan_name"], "requests_used": used,
                         "quota_limit": source["quota_limit"], "quota_period": source["quota_period"],
                         "quota_remaining": source["quota_limit"] - quota_used[quota_key],
                         "is_synthetic": True, "source_type": "synthetic_demo", "test_scenario": scenario,
                         "billing_status": "not_calculated"})
            monthly_totals[month][customer["customer_id"]] += used
            customer_totals[customer["customer_id"]] += used
        key = f"date={day.isoformat()}/usage.json"
        document = {"schema_version": 1, "dataset_id": DATASET_ID, "source_type": "synthetic_demo",
                    "is_synthetic": True, "usage_date": day.isoformat(), "timezone": "UTC",
                    "period_start": day.isoformat() + "T00:00:00Z",
                    "period_end_exclusive": (day + timedelta(days=1)).isoformat() + "T00:00:00Z",
                    "aws_region": source["aws_region"], "aws_account_id": source["aws_account_id"],
                    "record_count": len(rows), "total_requests": sum(row["requests_used"] for row in rows),
                    "records": rows}
        documents[key] = document
        files.append({"usage_date": day.isoformat(), "key": f"{S3_PREFIX}/{key}",
                      "sha256": hashlib.sha256(json_bytes(document)).hexdigest(),
                      "record_count": len(rows), "total_requests": document["total_requests"]})
        day += timedelta(days=1)
    manifest = {"schema_version": 1, "dataset_id": DATASET_ID, "source_type": "synthetic_demo",
                "is_synthetic": True, "status": "complete", "timezone": "UTC",
                "description": "Invented historical usage for UI/reporting tests; not API Gateway measurements or invoices.",
                "record_format": "normalized daily key/plan usage, not a raw GetUsage response",
                "start_date": START_DATE.isoformat(), "end_date": END_DATE.isoformat(),
                "bucket": source["bucket"], "prefix": S3_PREFIX, "daily_file_count": len(files),
                "record_count": sum(item["record_count"] for item in files),
                "expected_total_requests": sum(customer_totals.values()),
                "expected_customer_totals": customer_totals, "expected_monthly_customer_totals": monthly_totals,
                "customers": [{key: value for key, value in customer.items() if key != "base_requests"}
                              for customer in source["customers"]],
                "usage_plan_id": source["usage_plan_id"], "usage_plan_name": source["usage_plan_name"],
                "files": files}
    return documents, manifest


def save_locally(documents, manifest):
    """Input: generated documents and manifest. Save their exact upload bytes; return None."""
    for key, document in {**documents, "manifest.json": manifest}.items():
        destination = LOCAL_ROOT / key
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(json_bytes(document))


def upload_dataset(session, source, documents, manifest):
    """Upload/verify immutable daily files, then publish the complete manifest last.

    Inputs: SDK session, demo metadata, and generated documents. Return object count.
    Identical existing objects are verified and reused; differing objects are never
    overwritten. Every object uses AES256 encryption and synthetic-data metadata.
    Raise on ownership, permission, content-hash, encryption, or upload failures.
    """
    s3 = session.client("s3")
    bucket = source["bucket"]
    owner = source["aws_account_id"]
    s3.head_bucket(Bucket=bucket, ExpectedBucketOwner=owner)

    def upload_one(item):
        """Input: (relative key, JSON document). Return the verified S3 key after hash checking."""
        relative_key, document = item
        key = f"{S3_PREFIX}/{relative_key}"
        payload = json_bytes(document)
        expected_hash = hashlib.sha256(payload).hexdigest()
        try:
            existing = s3.get_object(Bucket=bucket, Key=key, ExpectedBucketOwner=owner)
        except ClientError as error:
            if error.response["Error"]["Code"] not in ("NoSuchKey", "404"):
                raise
            s3.put_object(Bucket=bucket, Key=key, Body=payload, ContentType="application/json",
                          ServerSideEncryption="AES256", ExpectedBucketOwner=owner, IfNoneMatch="*",
                          Metadata={"source-type": "synthetic_demo", "dataset-id": DATASET_ID,
                                    "is-synthetic": "true", "sha256": expected_hash})
            existing = s3.get_object(Bucket=bucket, Key=key, ExpectedBucketOwner=owner)
        actual_hash = hashlib.sha256(existing["Body"].read()).hexdigest()
        if actual_hash != expected_hash or existing.get("ServerSideEncryption") != "AES256":
            raise ValueError(f"Existing/uploaded object differs or is not encrypted: {key}")
        return key

    with ThreadPoolExecutor(max_workers=4) as pool:
        verified = list(pool.map(upload_one, documents.items()))
    upload_one(("manifest.json", manifest))
    return len(verified) + 1


def main():
    """Read CLI options, generate local files, and upload only when --upload is supplied.

    Prints dataset location, verified file count, and expected customer totals; returns None.
    Credentials, secret key values, prices, and fabricated native usage are never output.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stack", default=DEFAULT_STACK)
    parser.add_argument("--region", default=DEFAULT_REGION)
    parser.add_argument("--upload", action="store_true", help="Upload and read back the generated daily files and manifest")
    args = parser.parse_args()
    session = boto3.Session(region_name=args.region)
    source = load_demo_source(session, args.stack)
    documents, manifest = build_dataset(source)
    save_locally(documents, manifest)
    verified_count = upload_dataset(session, source, documents, manifest) if args.upload else 0
    print(json.dumps({"dataset_id": DATASET_ID, "start_date": manifest["start_date"], "end_date": manifest["end_date"],
                      "daily_files": len(documents), "records": manifest["record_count"],
                      "expected_total_requests": manifest["expected_total_requests"],
                      "customer_totals": manifest["expected_customer_totals"],
                      "verified_s3_objects": verified_count, "s3_uri": f"s3://{source['bucket']}/{S3_PREFIX}/",
                      "local_directory": str(LOCAL_ROOT)}, indent=2))


# Importing the generator never reads AWS or writes files; live work requires this CLI entrypoint.
if __name__ == "__main__":
    main()
