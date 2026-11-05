"""Publish report-download requests to SNS.

Inputs: the displayed report, selected file format, configured topic, and AWS client.
Output: SNS message ID. Failures propagate to the UI, which still allows the download.
These events describe clicks; they cannot prove that a browser finished saving a file.
"""

# Keep notification dependencies and supported formats above the publishing function.
from datetime import datetime, timezone
import json
from uuid import uuid4

DOWNLOAD_FORMATS = {"csv", "json"}


def publish_download_request(report, file_format, topic_arn, client):
    """Input: report dict, csv/json format, SNS topic ARN, and authenticated SNS client.

    Return the message ID only after SNS accepts the event. Include customer/report
    metadata, UTC request time, and an event ID; omit credentials and report contents.
    A customer identifies the report, not the anonymous person clicking Download.
    SNS delivery is best effort from this UI; subscribers may receive duplicates.
    """
    if file_format not in DOWNLOAD_FORMATS:
        raise ValueError("Unsupported report download format")
    if not topic_arn:
        raise ValueError("Report-download SNS topic is not configured")
    event = {
        "event_type": "report_download_requested",
        "event_id": str(uuid4()),
        "requested_at_utc": datetime.now(timezone.utc).isoformat(),
        "customer_id": report["customer_id"],
        "customer_name": report["customer_name"],
        "start_date": report["start_date"],
        "end_date": report["end_date"],
        "total_requests": report["total_requests"],
        "file_format": file_format,
        "source": report["source"],
        "dataset_id": report.get("dataset_id"),
        "requester_identity": "anonymous",
        "download_completion_verified": False,
    }
    response = client.publish(
        TopicArn=topic_arn,
        Subject="API usage report download requested",
        Message=json.dumps(event, ensure_ascii=False, indent=2),
    )
    return response["MessageId"]
