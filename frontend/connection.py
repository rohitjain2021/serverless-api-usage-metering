"""Resolve frontend source settings and create an S3 client without exposing credentials.

Inputs: Streamlit Secrets mapping (optional) and standard environment variables.
Outputs: validated non-secret source settings and an authenticated AWS SDK client.
"""

# Read deployment-provided settings while preserving the normal local AWS credential chain.
import os
import boto3
from botocore.config import Config

# Keep timeouts bounded so a missing or misconfigured connection fails visibly in the UI.
S3_CLIENT_CONFIG = Config(connect_timeout=5, read_timeout=20,
                          retries={"mode": "standard", "total_max_attempts": 4}, max_pool_connections=10)


def connection_settings(secrets=None):
    """Input: optional Streamlit Secrets/dict. Return source, bucket, prefix, region, and AWS section.

    USAGE_DATA_SOURCE/USAGE_S3_BUCKET/USAGE_S3_PREFIX/AWS_DEFAULT_REGION override
    the corresponding [usage] settings. Credentials are never returned in a report or logged.
    """
    secrets = secrets or {}
    usage = secrets.get("usage", {})
    return {"source": os.environ.get("USAGE_DATA_SOURCE", usage.get("source", "local")),
            "bucket": os.environ.get("USAGE_S3_BUCKET", usage.get("bucket", "")),
            "prefix": os.environ.get("USAGE_S3_PREFIX", usage.get("prefix", "")),
            "region": os.environ.get("AWS_DEFAULT_REGION", usage.get("region", "us-east-1")),
            "download_topic_arn": os.environ.get("USAGE_DOWNLOAD_TOPIC_ARN", usage.get("download_topic_arn", "")),
            "aws": secrets.get("aws", {})}


def s3_client(settings):
    """Input: connection settings. Return an S3 client using Secrets or SDK environment/profile credentials.

    Explicit access_key_id/secret_access_key must be supplied together. session_token
    is supported for temporary credentials. Missing credentials surface as SDK errors
    when reading S3. No access keys are created or persisted by this function.
    """
    return aws_client("s3", settings)


def aws_client(service, settings):
    """Inputs: AWS service name and connection settings. Return a client using the same frontend identity.

    Explicit credentials must be supplied together; otherwise use the SDK credential chain.
    Credential values are used only to authenticate and are never included in notifications.
    """
    aws = settings["aws"]
    key_id = aws.get("access_key_id")
    key_secret = aws.get("secret_access_key")
    if bool(key_id) != bool(key_secret):
        raise ValueError("Supply both AWS access key fields, or use the normal AWS credential configuration")
    kwargs = {"region_name": settings["region"]}
    if key_id:
        kwargs.update(aws_access_key_id=key_id, aws_secret_access_key=key_secret)
        if aws.get("session_token"):
            kwargs["aws_session_token"] = aws["session_token"]
    return boto3.Session(**kwargs).client(service, config=S3_CLIENT_CONFIG)
