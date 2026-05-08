"""AWS Lambda entrypoint. One Lambda per vendor; selected by VENDOR env var.

Native ingest path: forwarder writes gzipped JSON-lines to S3 (under a
{vendor}/ prefix), S3 ObjectCreated notifies SQS, XSIAM pulls.
"""

from __future__ import annotations

import json
import logging
import os

import boto3

from .core import run
from .egress.s3 import S3Egress
from .state_aws import DynamoStateStore
from .vendors import AuditClient
from .vendors.anthropic_chat_content import AnthropicChatContentClient
from .vendors.anthropic_compliance import AnthropicComplianceClient
from .vendors.openai_audit import OpenAIAuditClient
from .vendors.openai_conversations import OpenAIConversationsClient

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

_secrets = None


def _secret(name: str) -> str:
    global _secrets
    if _secrets is None:
        _secrets = boto3.client("secretsmanager")
    return _secrets.get_secret_value(SecretId=name)["SecretString"]


def _make_client(vendor: str, api_key_secret_arn: str) -> AuditClient:
    api_key = _secret(api_key_secret_arn)
    if vendor == "anthropic":
        return AnthropicComplianceClient(api_key)
    if vendor == "anthropic_chats":
        return AnthropicChatContentClient(api_key)
    if vendor == "openai":
        return OpenAIAuditClient(api_key)
    if vendor == "openai_conversations":
        return OpenAIConversationsClient(api_key)
    raise ValueError(
        f"Unsupported VENDOR={vendor!r}. Supported: "
        "'anthropic', 'anthropic_chats', 'openai', 'openai_conversations'."
    )


def handler(event, context):
    vendor = os.environ["VENDOR"]
    table = os.environ["STATE_TABLE"]
    region = os.environ.get("AWS_REGION")
    api_key_secret_arn = os.environ["API_KEY_SECRET_ARN"]
    bucket = os.environ["AUDIT_BUCKET"]
    prefix = os.environ.get("AUDIT_PREFIX", "audit")
    lookback = int(os.environ.get("INITIAL_LOOKBACK_MINUTES", "60"))

    client = _make_client(vendor, api_key_secret_arn)
    egress = S3Egress(bucket=bucket, vendor=vendor, prefix=prefix)
    store = DynamoStateStore(vendor=vendor, table_name=table, region=region)

    summary = run(client, egress, store, initial_lookback_minutes=lookback)
    log.info("summary=%s", summary)
    return {"statusCode": 200, "body": json.dumps(summary)}
