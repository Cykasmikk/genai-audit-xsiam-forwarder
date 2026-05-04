"""AWS Lambda entrypoint. Native ingest path: S3 → SQS → XSIAM."""

from __future__ import annotations

import json
import logging
import os

import boto3

from .claude_client import ClaudeComplianceClient
from .core import run
from .egress.s3 import S3Egress
from .state_aws import DynamoStateStore

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

_secrets = None


def _secret(name: str) -> str:
    # Lazy: avoids resolving an AWS region at import time. In Lambda, AWS_REGION
    # is always set; the client is constructed once per container.
    global _secrets
    if _secrets is None:
        _secrets = boto3.client("secretsmanager")
    return _secrets.get_secret_value(SecretId=name)["SecretString"]


def handler(event, context):
    table = os.environ["STATE_TABLE"]
    region = os.environ.get("AWS_REGION")
    anthropic_secret = os.environ["ANTHROPIC_KEY_SECRET_ARN"]
    bucket = os.environ["AUDIT_BUCKET"]
    prefix = os.environ.get("AUDIT_PREFIX", "claude-compliance")
    lookback = int(os.environ.get("INITIAL_LOOKBACK_MINUTES", "60"))

    claude = ClaudeComplianceClient(_secret(anthropic_secret))
    egress = S3Egress(bucket=bucket, prefix=prefix)
    store = DynamoStateStore(table, region=region)

    summary = run(claude, egress, store, initial_lookback_minutes=lookback)
    log.info("summary=%s", summary)
    return {"statusCode": 200, "body": json.dumps(summary)}
