"""Adapt the monthly Lambda sample into customer/date-range reports for the UI.

Inputs: the existing fixture, a stable customer ID, and inclusive UTC calendar dates.
Outputs: customer summaries and downloadable CSV/JSON bytes, without AWS access.
"""

# Parse the sample and serialize the detailed download.
import json
# Convert ISO dates and isolate the sample runner's temporary report files.
from datetime import date
from pathlib import Path
import tempfile
# Serialize the runner's temporary client/environment substitutions across UI sessions.
from threading import Lock

# Reuse the actual Lambda sample invocation and its spreadsheet-safe CSV formatter.
from scripts.run_sample_report import DEFAULT_SAMPLE, run_sample
from src.app import FIELDS, csv_report

# The local runner patches the handler factory temporarily, so only one run may do so at a time.
SAMPLE_RUN_LOCK = Lock()
# Include the selected dates in every CSV row so exported totals retain their context.
DOWNLOAD_FIELDS = ("start_date", "end_date", *FIELDS)


def available_source(sample_path=DEFAULT_SAMPLE):
    """Read customer choices and available dates from one local sample.

    Input: optional fixture path. Return a dict with customers, start_date, end_date,
    and region. Customer choices are deduplicated by stable customer_id.
    Raise ValueError for ambiguous customer names, absent customers, or mixed periods.
    """
    sample = json.loads(Path(sample_path).read_text(encoding="utf-8"))
    customers = {}
    for key in sample["get_api_keys_response"]["items"]:
        tags = key.get("tags") or {}
        customer_id = tags.get("customer_id", "").strip()
        if not customer_id:
            continue
        name = tags.get("customer_name", "").strip() or customer_id
        if customer_id in customers and customers[customer_id] != name:
            raise ValueError(f"Conflicting customer names for {customer_id}")
        customers[customer_id] = name
    periods = {(usage["startDate"], usage["endDate"])
               for usage in sample["get_usage_responses_by_plan_id"].values()}
    if not customers or len(periods) != 1:
        raise ValueError("The sample must contain tagged customers and one common reporting period")
    start, end = next(iter(periods))
    return {"customers": dict(sorted(customers.items(), key=lambda item: (item[1], item[0]))),
            "start_date": date.fromisoformat(start), "end_date": date.fromisoformat(end),
            "region": sample["region"]}


def generate_customer_report(customer_id, start_date, end_date, sample_path=DEFAULT_SAMPLE):
    """Run the sample Lambda and select one customer's inclusive date range.

    Args:
        customer_id (str): A tagged customer from available_source.
        start_date/end_date (date): Inclusive dates inside the sample's available period.
        sample_path: Optional fixture location, shared with the sample runner.

    Returns:
        dict: Customer identity, dates, request/key/plan totals, and filtered key rows.
        Multiple keys for the same customer contribute to the overall request total.

    Raises:
        ValueError: Unknown customer, reversed dates, or dates with no source coverage.
        Sample/SDK/file errors: Propagate failures rather than display invented totals.
    """
    source = available_source(sample_path)
    if customer_id not in source["customers"]:
        raise ValueError("Select a customer present in the sample data")
    if not isinstance(start_date, date) or not isinstance(end_date, date):
        raise ValueError("Select both a start date and an end date")
    if start_date > end_date:
        raise ValueError("Start date must be on or before end date")
    if start_date < source["start_date"] or end_date > source["end_date"]:
        raise ValueError("Selected dates must stay within the available sample period")
    # Invoke the real monthly handler once, then read its exact JSON upload payload.
    # Date-range selection is a UI service operation; Lambda's monthly contract is unchanged.
    with SAMPLE_RUN_LOCK, tempfile.TemporaryDirectory() as directory:
        invocation = run_sample(sample_path, directory)
        report_path = next(Path(path) for path in invocation["local_files"] if path.endswith(".json"))
        monthly_report = json.loads(report_path.read_text(encoding="utf-8"))
    usage = []
    for row in monthly_report["usage"]:
        if row["customer_id"] != customer_id:
            continue
        daily = [entry for entry in row["daily_usage"]
                 if start_date <= date.fromisoformat(entry["date"]) <= end_date]
        usage.append({**row, "start_date": start_date.isoformat(), "end_date": end_date.isoformat(),
                      "requests_used": sum(entry["requests_used"] for entry in daily), "daily_usage": daily})
    return {"schema_version": 2, "report_type": "customer_date_range", "source": "local_sample",
            "source_month": monthly_report["month"], "region": source["region"],
            "customer_id": customer_id, "customer_name": source["customers"][customer_id],
            "start_date": start_date.isoformat(), "end_date": end_date.isoformat(),
            "days_selected": (end_date - start_date).days + 1,
            "total_requests": sum(row["requests_used"] for row in usage),
            "api_key_count": len({row["api_key_id"] for row in usage}),
            "usage_plan_count": len({row["usage_plan_id"] for row in usage}), "usage": usage}


def downloads(report):
    """Input: customer report dict. Return (CSV bytes, JSON bytes) for UI downloads.

    CSV uses the backend formatter's formula escaping and includes the selected dates.
    """
    return (csv_report(report, fields=DOWNLOAD_FIELDS),
            json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8"))
