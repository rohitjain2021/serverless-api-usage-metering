"""Invoke the real Lambda handler with one local API Gateway fixture.

Input: samples/api-gateway-usage.json by default, optionally overridden on the CLI.
Output: the handler's monthly CSV/JSON payloads saved below output/sample-reports/.
Botocore validates and supplies the API responses; a local sink replaces S3.
No AWS requests are made, and no additional dependencies are required.
"""

# Read command-line paths and print the Lambda's completion summary.
import argparse
import json
# Set scoped environment variables and replace only the handler's client factory.
import os
from unittest.mock import patch
# Locate the repository when the runner is launched from any working directory.
from pathlib import Path
import sys

# Validate sample responses against the same SDK models used by real API calls.
import boto3
from botocore.stub import Stubber

# Define shared paths before loading the application's existing handler.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SAMPLE = PROJECT_ROOT / "samples" / "api-gateway-usage.json"
DEFAULT_OUTPUT = PROJECT_ROOT / "output" / "sample-reports"
LOCAL_BUCKET = "local-sample-reports"

# Make src importable when executing this script directly rather than as a module.
sys.path.insert(0, str(PROJECT_ROOT))
from src import app


class LocalReportSink:
    """Save S3 put_object payloads on disk without contacting S3.

    Input: destination directory at construction, followed by handler upload calls.
    Output: local files preserving the handler's S3 key paths; saved paths are recorded.
    """

    def __init__(self, output_dir):
        """Input: output directory path. Initialize resolved root and empty file list.

        Returns None; directory creation is deferred until an upload is received.
        """
        self.root = Path(output_dir).resolve()
        self.saved_paths = []

    def put_object(self, *, Bucket, Key, Body, ContentType, ServerSideEncryption):
        """Accept the handler's S3 upload arguments and write Body bytes locally.

        Inputs: bucket name, object key, serialized bytes, MIME type, and encryption.
        Returns: empty dict, since the handler does not consume the S3 response.
        Raises ValueError for an unexpected destination or unsafe output path.
        Encryption is checked as an upload parameter; local files are plain text.
        """
        if Bucket != LOCAL_BUCKET or ServerSideEncryption != app.S3_ENCRYPTION:
            raise ValueError("Unexpected sample upload destination or encryption setting")
        destination = (self.root / Key).resolve()
        if not destination.is_relative_to(self.root):
            raise ValueError("Report path must stay inside the selected output directory")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(Body)
        self.saved_paths.append(destination)
        return {}


def run_sample(sample_path=DEFAULT_SAMPLE, output_dir=DEFAULT_OUTPUT):
    """Feed one fixture through lambda_handler and save its actual upload payloads.

    Args:
        sample_path: JSON containing month, region, get_usage_plans_response,
            get_api_keys_response, and get_usage_responses_by_plan_id. The envelope
            names identify SDK calls; their contents preserve the AWS field names.
        output_dir: Root directory for the simulated S3 object files.

    Returns:
        dict: Lambda summary plus local_files identifying the generated artifacts.

    Raises:
        ValueError: Fixture dates or daily entry counts do not cover the selected month.
        SDK stub errors: A response shape or requested API call differs from the fixture.
        File/JSON errors: The fixture cannot be read or report files cannot be written.
    """
    sample = json.loads(Path(sample_path).read_text(encoding="utf-8"))
    event = {"month": sample["month"]}
    start, end = app.report_period(event)
    # Explicit dummy credentials avoid querying any local or AWS credential provider.
    session = boto3.Session(aws_access_key_id="sample-access-key",
                            aws_secret_access_key="sample-secret-key",
                            region_name=sample["region"])
    api = session.client("apigateway")
    sink = LocalReportSink(output_dir)
    with Stubber(api) as stub:
        # Queue responses in the same order the production collector requests them.
        stub.add_response("get_usage_plans", sample["get_usage_plans_response"], {"limit": app.PAGE_SIZE})
        stub.add_response("get_api_keys", sample["get_api_keys_response"],
                          {"limit": app.PAGE_SIZE, "includeValues": False})
        for plan in sorted(sample["get_usage_plans_response"]["items"], key=lambda item: item["id"]):
            usage = sample["get_usage_responses_by_plan_id"][plan["id"]]
            if (usage["usagePlanId"] != plan["id"] or usage["startDate"] != start.isoformat()
                    or usage["endDate"] != end.isoformat()):
                raise ValueError("Sample usage plan and dates must match the requested month")
            if any(len(daily) != (end - start).days + 1 for daily in usage["items"].values()):
                raise ValueError("Sample usage must contain every day of the requested month")
            stub.add_response("get_usage", usage,
                              {"limit": app.PAGE_SIZE, "usagePlanId": plan["id"],
                               "startDate": start.isoformat(), "endDate": end.isoformat()})
        # Scope the local substitutions to this invocation; production stays AWS-backed.
        with patch.dict(os.environ, {"REPORT_BUCKET": LOCAL_BUCKET, "REPORT_PREFIX": "reports",
                                     "USAGE_PLAN_IDS": ""}), patch.object(
                app, "create_clients", return_value=(api, sink)):
            result = app.lambda_handler(event, None)
        stub.assert_no_pending_responses()
    return {**result, "local_files": [str(path) for path in sink.saved_paths]}


def main():
    """Input: optional --sample/--output CLI paths. Print summary; return None.

    Any error propagates to the terminal with a nonzero process exit status.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", type=Path, default=DEFAULT_SAMPLE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    arguments = parser.parse_args()
    print(json.dumps(run_sample(arguments.sample, arguments.output), indent=2))


# Run only when launched explicitly, not when imported by the integration test.
if __name__ == "__main__":
    main()
