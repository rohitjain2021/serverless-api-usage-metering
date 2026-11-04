"""Generate a bounded set of real demo API calls or fetch a report without new traffic.

Inputs: --stack/--region identify the deployed demo; --report-only skips traffic.
Outputs: local traffic receipts or actual Lambda-generated CSV/JSON downloaded from S3.
Uses normal AWS credentials; API key secrets stay in memory and are never printed/saved.
"""

# Parse options, record non-secret evidence, and pace requests below the demo rate target.
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit
import urllib.request

# Access stack outputs, key metadata, Lambda, and S3 through the existing pinned SDK.
import boto3

# Keep the demo bounded and repeatable: 30 accepted requests at most per invocation.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_ROOT = PROJECT_ROOT / "output" / "live-demo"
DEFAULT_STACK = "serverless-api-usage-metering-dev"
DEFAULT_REGION = "us-east-1"
CUSTOMER_REQUESTS = (
    ("AcmeDemoApiKeyId", "cust-001", "Acme Ltd", 5),
    ("NorthwindDemoApiKeyId", "cust-002", "Northwind Ltd", 10),
    ("ContosoDemoApiKeyId", "cust-003", "Contoso Ltd", 15),
)
REQUEST_INTERVAL_SECONDS = 0.35

# Reuse the collector's pagination behavior when recording native usage before the run.
sys.path.insert(0, str(PROJECT_ROOT))
from src.app import pages


def deployed_outputs(session, stack_name):
    """Input: SDK session and stack name. Return outputs only for a completed demo stack.

    Raise ValueError if resources are not ready or the endpoint does not match the
    returned API ID/region. This prevents sending a generated key to another host.
    """
    stack = session.client("cloudformation").describe_stacks(StackName=stack_name)["Stacks"][0]
    if stack["StackStatus"] not in ("CREATE_COMPLETE", "UPDATE_COMPLETE"):
        raise ValueError("Demo stack must be successfully deployed before testing")
    outputs = {entry["OutputKey"]: entry["OutputValue"] for entry in stack["Outputs"]}
    target = urlsplit(outputs["DemoApiUrl"])
    expected_host = f"{outputs['DemoRestApiId']}.execute-api.{session.region_name}.amazonaws.com"
    if target.scheme != "https" or target.hostname != expected_host or not target.path.endswith("/usage-demo"):
        raise ValueError("Stack output does not identify the expected AWS demo endpoint")
    return outputs


def send_demo_requests(session, outputs):
    """Send 5/10/15 real requests for the three tagged customers and save a receipt.

    Inputs: authenticated SDK session and verified stack outputs.
    Return: receipt containing initial native counts, accepted requests, and request IDs.
    Files are updated after every success, so a partial failure retains its evidence.
    Rerunning this function sends another batch; use --report-only to avoid extra traffic.
    """
    api = session.client("apigateway")
    started = datetime.now(timezone.utc)
    day = started.date().isoformat()
    baseline = {}
    for page in pages(api.get_usage, usagePlanId=outputs["DemoUsagePlanId"], startDate=day, endDate=day):
        baseline.update({key_id: sum(pair[0] for pair in daily) for key_id, daily in page.get("items", {}).items()})
    receipt = {"started_at": started.isoformat(), "date": day, "usage_plan_id": outputs["DemoUsagePlanId"],
               "native_counts_before_run": baseline, "customers": [], "accepted_requests": 0}
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    receipt_path = OUTPUT_ROOT / f"traffic-receipt-{started.strftime('%Y%m%dT%H%M%S%fZ')}.json"
    for output_name, customer_id, customer_name, count in CUSTOMER_REQUESTS:
        key = api.get_api_key(apiKey=outputs[output_name], includeValue=True)
        if key.get("tags", {}).get("customer_id") != customer_id:
            raise ValueError("Deployed customer tags differ from the expected demo identities")
        customer = {"customer_id": customer_id, "customer_name": customer_name,
                    "api_key_id": key["id"], "requested_count": count, "accepted_count": 0, "request_ids": []}
        receipt["customers"].append(customer)
        for index in range(count):
            request = urllib.request.Request(outputs["DemoApiUrl"], headers={"x-api-key": key["value"]})
            with urllib.request.urlopen(request, timeout=15) as response:
                payload = json.load(response)
                if response.status != 200:
                    raise RuntimeError("Demo request did not succeed")
            customer["request_ids"].append(payload["requestId"])
            customer["accepted_count"] += 1
            receipt["accepted_requests"] += 1
            receipt_path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
            time.sleep(REQUEST_INTERVAL_SECONDS)
        # Only identifiers, customer names, and counts enter the receipt and terminal output.
        del key
        print(f"{customer_name}: {count} accepted requests", flush=True)
    receipt["finished_at"] = datetime.now(timezone.utc).isoformat()
    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print(f"Receipt: {receipt_path}")
    return receipt


def fetch_today_report(session, outputs):
    """Invoke the real Lambda for today's UTC date and download its S3 report objects.

    Inputs: SDK session and stack outputs. Returns the Lambda summary plus per-customer
    totals and local paths. No traffic is generated. Raise on Lambda/storage failure.
    Today's counts are provisional and may need another invocation after metering catches up.
    """
    day = datetime.now(timezone.utc).date().isoformat()
    event = {"start_date": day, "end_date": day}
    response = session.client("lambda").invoke(FunctionName=outputs["ReportFunctionName"],
                                                Payload=json.dumps(event).encode("utf-8"))
    result = json.load(response["Payload"])
    if response.get("FunctionError"):
        raise RuntimeError("Deployed Lambda failed: " + json.dumps(result))
    if result["bucket"] != outputs["ReportsBucketName"]:
        raise ValueError("Lambda returned an unexpected report bucket")
    s3 = session.client("s3")
    paths = []
    customer_totals = {}
    for key in result["keys"]:
        destination = (OUTPUT_ROOT / key).resolve()
        if not destination.is_relative_to(OUTPUT_ROOT.resolve()):
            raise ValueError("Downloaded report path leaves the local output directory")
        stored = s3.get_object(Bucket=result["bucket"], Key=key)
        if stored.get("ServerSideEncryption") != "AES256":
            raise ValueError("Expected an encrypted S3 report")
        data = stored["Body"].read()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        paths.append(str(destination))
        if key.endswith(".json"):
            report = json.loads(data)
            for row in report["usage"]:
                customer_totals[row["customer_id"]] = customer_totals.get(row["customer_id"], 0) + row["requests_used"]
    return {**result, "customer_totals": customer_totals, "local_files": paths}


def main():
    """Input: CLI options. Send one batch or print the fetched report summary; return None.

    AWS/HTTP failures propagate with a nonzero exit; no automatic traffic retries occur.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stack", default=DEFAULT_STACK)
    parser.add_argument("--region", default=DEFAULT_REGION)
    parser.add_argument("--report-only", action="store_true", help="Fetch today's report without sending any new demo requests")
    arguments = parser.parse_args()
    session = boto3.Session(region_name=arguments.region)
    outputs = deployed_outputs(session, arguments.stack)
    if arguments.report_only:
        print(json.dumps(fetch_today_report(session, outputs), indent=2))
    else:
        send_demo_requests(session, outputs)


# Make live traffic an explicit CLI invocation, never an import/test side effect.
if __name__ == "__main__":
    main()
