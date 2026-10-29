"""Collect monthly API Gateway REST API usage and publish CSV/JSON reports to S3.

Input: a Lambda event containing an optional historical ``month`` or scheduled
``time``, plus deployment settings supplied through environment variables.
Output: two encrypted S3 objects and a dictionary summarizing the completed run.
The collection/formatting helpers can also be called separately in local tests.
"""

# Find the final day of a month, including leap years.
import calendar
# Serialize summary rows with correct quoting for spreadsheet files.
import csv
# Build the CSV in memory before uploading it.
import io
# Serialize the detailed report and structured log messages.
import json
# Send completion messages to Lambda's configured log destination.
import logging
# Read bucket, prefix, and plan selection from environment variables.
import os
# Validate the YYYY-MM shape of a manually requested report month.
import re
# Calculate UTC report dates.
from datetime import date, datetime, timedelta, timezone

# Create AWS API Gateway and S3 SDK clients.
import boto3
# Configure SDK timeouts and transient retries.
from botocore.config import Config


# Module logger; never logs API key values.
LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.INFO)

# Shared configuration. Invocation data and environment values are read per run.
# Maximum number of API Gateway results requested per page.
PAGE_SIZE = 500
# S3 folder used unless REPORT_PREFIX is set.
DEFAULT_REPORT_PREFIX = "reports"
# Request S3-managed server-side encryption for uploads.
S3_ENCRYPTION = "AES256"
# Month label used in report contents and S3 paths.
MONTH_FORMAT = "%Y-%m"
# Date parsing also checks calendar validity.
MONTH_PATTERN = re.compile(r"\d{4}-\d{2}")
# Escape these in user-supplied names.
CSV_FORMULA_PREFIXES = ("=", "+", "-", "@")
# Ordered CSV columns; detailed daily records appear only in the JSON report.
FIELDS = ("month", "usage_plan_id", "usage_plan_name", "api_key_id", "api_key_name", "requests_used")
# Apply the same retry and timeout settings to both AWS clients.
AWS_CONFIG = Config(
    retries={"mode": "standard", "total_max_attempts": 8},
    connect_timeout=5,
    read_timeout=30,
)


def report_period(event, now=None):
    """Resolve the inclusive start and end dates of a completed report month.

    Args:
        event (dict): Optional ``month`` as YYYY-MM, or ``time`` as an ISO 8601
            timestamp with a timezone. An explicit month takes precedence.
        now (datetime | None): Reference time, normally UTC; defaults to current
            UTC time. Tests can supply a fixed time. A scheduled time overrides
            this reference when no explicit month is supplied.

    Returns:
        tuple[date, date]: First and last day of the selected month. Without an
        explicit month, selects the month before the reference time.

    Raises:
        ValueError: The date is invalid, the scheduled time lacks a timezone,
            or the month has not finished relative to the reference time.
    """
    now = now or datetime.now(timezone.utc)
    if "month" in event:
        month = event["month"]
        if not isinstance(month, str) or not MONTH_PATTERN.fullmatch(month):
            raise ValueError("month must be YYYY-MM")
        start = date.fromisoformat(month + "-01")
    else:
        # Preserve the intended reporting month if a scheduled invocation is delayed.
        scheduled = event.get("time")
        if scheduled is not None:
            now = datetime.fromisoformat(scheduled.replace("Z", "+00:00"))
            if now.tzinfo is None:
                raise ValueError("time must include a timezone")
            now = now.astimezone(timezone.utc)
        # Step back from this month's first day, then select that month's first day.
        start = (now.date().replace(day=1) - timedelta(days=1)).replace(day=1)
    end = start.replace(day=calendar.monthrange(start.year, start.month)[1])
    if end >= now.date():
        raise ValueError("Only completed months can be reported")
    return start, end


def pages(operation, **kwargs):
    """Iterate through all pages returned by one API Gateway SDK operation.

    Args:
        operation (callable): SDK method accepting ``limit`` and ``position``.
        **kwargs: Operation-specific arguments, such as usagePlanId and dates.

    Yields:
        dict: Each complete SDK response, including its items and pagination token.

    Raises:
        RuntimeError: A repeated continuation token would cause an endless loop.
        SDK exceptions: AWS failures propagate to the caller after SDK retries.
    """
    seen = set()
    while True:
        response = operation(limit=PAGE_SIZE, **kwargs)
        yield response
        position = response.get("position")
        if not position:
            return
        if position in seen:
            raise RuntimeError("API Gateway returned a repeated pagination token")
        seen.add(position)
        kwargs["position"] = position


def collect_report(api, start, end, plan_ids=()):
    """Fetch API Gateway usage and assemble a report without writing any files.

    Args:
        api: Boto3 API Gateway client, or a mock with the same methods.
        start (date): Inclusive first day of the reporting period.
        end (date): Inclusive final day of the reporting period.
        plan_ids (iterable[str]): Plan IDs to include; empty means all regional plans.

    Returns:
        dict: Schema version, month, inclusive dates, plan/row counts, total used
        requests, and a ``usage`` list with one row per returned key/plan pair.
        Each row includes names, IDs, its request total, and dated daily records.
        Missing key metadata produces an empty name; no results produce zero totals.

    Raises:
        ValueError: A selected plan is missing or daily usage is malformed.
        RuntimeError: Duplicate keys or pagination tokens make the data ambiguous.
        SDK exceptions: Collection fails rather than publishing incomplete results.
    """
    # Discover plans first so explicitly requested but missing plans fail clearly.
    plans = [plan for page in pages(api.get_usage_plans) for plan in page.get("items", [])]
    if plan_ids:
        missing = set(plan_ids) - {plan["id"] for plan in plans}
        if missing:
            raise ValueError("Configured usage plans were not found: " + ", ".join(sorted(missing)))
        plans = [plan for plan in plans if plan["id"] in plan_ids]
    # Build a name lookup using key IDs. Secret API key values are not requested.
    keys = {key["id"]: key.get("name", "")
            for page in pages(api.get_api_keys, includeValues=False)
            for key in page.get("items", [])}
    rows = []
    for plan in sorted(plans, key=lambda item: item["id"]):
        seen_keys = set()
        for page in pages(api.get_usage, usagePlanId=plan["id"],
                          startDate=start.isoformat(), endDate=end.isoformat()):
            for key_id, daily in sorted(page.get("items", {}).items()):
                # Do not silently double-count or overwrite an unexpected repeated key.
                if key_id in seen_keys:
                    raise RuntimeError("Duplicate API key in usage pages; refusing an ambiguous report")
                seen_keys.add(key_id)
                if len(daily) > (end - start).days + 1:
                    raise ValueError("Usage data exceeds the report period")
                # Each daily entry is [used requests, remaining quota]. Only used
                # requests are summed; remaining quota may be negative after overuse.
                if any(len(pair) != 2 or any(type(value) is not int for value in pair)
                       or pair[0] < 0 for pair in daily):
                    raise ValueError("Invalid daily usage data")
                # API Gateway's daily entries are ordered from the requested start date.
                rows.append({"month": start.strftime(MONTH_FORMAT),
                             "usage_plan_id": plan["id"], "usage_plan_name": plan.get("name", ""),
                             "api_key_id": key_id, "api_key_name": keys.get(key_id, ""),
                             "requests_used": sum(pair[0] for pair in daily),
                             "daily_usage": [{"date": (start + timedelta(days=index)).isoformat(),
                                              "requests_used": pair[0], "quota_remaining": pair[1]}
                                             for index, pair in enumerate(daily)]})
    # Stable ordering makes repeated reports easy to compare across runs.
    rows.sort(key=lambda row: (row["usage_plan_id"], row["api_key_id"]))
    return {"schema_version": 1, "month": start.strftime(MONTH_FORMAT),
            "start_date": start.isoformat(), "end_date": end.isoformat(),
            "usage_plan_count": len(plans), "api_key_plan_count": len(rows),
            "total_requests": sum(row["requests_used"] for row in rows), "usage": rows}


def csv_report(report):
    """Convert the report's summary rows into an in-memory CSV payload.

    Args:
        report (dict): collect_report output containing a ``usage`` list.

    Returns:
        bytes: UTF-8 CSV with FIELDS as its header, even for an empty report.
        Daily records are omitted. Formula-like strings are prefixed with an
        apostrophe so spreadsheet software treats them as text.
    """
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=FIELDS, extrasaction="ignore")
    writer.writeheader()
    for row in report["usage"]:
        # Names are user-controlled; prevent spreadsheet formula execution.
        writer.writerow({key: "'" + value if isinstance(value, str) and
                         value.lstrip().startswith(CSV_FORMULA_PREFIXES) else value
                         for key, value in row.items()})
    return output.getvalue().encode("utf-8")


def create_clients():
    """Create AWS SDK clients without making report-related API calls.

    Inputs: no arguments; Boto3 resolves region and credentials from its normal
    configuration sources, including the Lambda execution environment.
    Returns: (API Gateway client, S3 client), both using AWS_CONFIG.
    Raises: SDK configuration errors, such as a missing region.
    """
    return boto3.client("apigateway", config=AWS_CONFIG), boto3.client("s3", config=AWS_CONFIG)


def lambda_handler(event, context):
    """Collect one month's usage, upload both reports, and summarize the result.

    Args:
        event (dict | None): {} or None selects the previous month; ``month``
            selects YYYY-MM; ``time`` supplies the original scheduled timestamp.
        context: AWS Lambda invocation metadata; accepted but currently unused.

    Environment:
        REPORT_BUCKET: Required destination bucket name.
        REPORT_PREFIX: Destination prefix; defaults to ``reports``.
        USAGE_PLAN_IDS: Optional comma-separated plan IDs; empty selects all.

    Returns:
        dict: Report month, bucket name, the two S3 object keys, total requests,
        and the number of API key/plan rows. Returned only after both uploads succeed.

    Side effects:
        Reads API Gateway data, uploads encrypted CSV and JSON objects, and logs
        completion. Reruns use the same object keys. Uploads are separate, so a
        failure on the second upload can leave the first object in S3.

    Raises:
        ValueError: Invalid event, destination settings, reporting month, or data.
        RuntimeError: Ambiguous paginated data.
        SDK exceptions: AWS read/write failures propagate so Lambda records failure.
    """
    event = {} if event is None else event
    if not isinstance(event, dict):
        raise ValueError("event must be a JSON object")
    # Read settings per invocation so tests and warm Lambda runs use current values.
    bucket = os.environ.get("REPORT_BUCKET", "").strip()
    if not bucket:
        raise ValueError("REPORT_BUCKET is required")
    prefix = os.environ.get("REPORT_PREFIX", DEFAULT_REPORT_PREFIX).strip("/")
    if not prefix:
        raise ValueError("REPORT_PREFIX must not be empty")
    start, end = report_period(event)
    # Ignore empty comma-separated entries and remove duplicate plan IDs.
    plan_ids = {value.strip() for value in os.environ.get("USAGE_PLAN_IDS", "").split(",") if value.strip()}
    api, s3 = create_clients()
    # Finish collection before publishing either output to avoid partial source data.
    report = collect_report(api, start, end, plan_ids)
    report["region"] = api.meta.region_name
    # Deterministic paths allow backfills/retries to replace the same month's reports.
    base = f"{prefix}/{report['month']}/usage"
    objects = [(base + ".csv", csv_report(report), "text/csv; charset=utf-8"),
               (base + ".json", json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8"), "application/json")]
    # Upload UTF-8 payloads with the correct MIME type and explicit S3 encryption.
    for key, body, content_type in objects:
        s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType=content_type,
                      ServerSideEncryption=S3_ENCRYPTION)
    # Return a small operational summary rather than the full customer usage dataset.
    result = {"month": report["month"], "bucket": bucket,
              "keys": [item[0] for item in objects],
              "total_requests": report["total_requests"], "api_key_plan_count": report["api_key_plan_count"]}
    LOGGER.info("Usage report complete: %s", json.dumps(result))
    return result
