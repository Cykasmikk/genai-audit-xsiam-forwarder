"""GCP Cloud Function (Gen 2) handler. One function per vendor; selected by
VENDOR env var. Re-exported by /src/main.py."""

from __future__ import annotations

import logging
import os

import functions_framework

from .core import run
from .egress.pubsub import PubSubEgress
from .state_gcp import FirestoreStateStore
from .vendors import AuditClient
from .vendors.anthropic_chat_content import AnthropicChatContentClient
from .vendors.anthropic_compliance import AnthropicComplianceClient
from .vendors.openai_audit import OpenAIAuditClient
from .vendors.openai_conversations import OpenAIConversationsClient

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


def _secret(resource: str) -> str:
    from google.cloud import secretmanager  # deferred for dev import

    client = secretmanager.SecretManagerServiceClient()
    return client.access_secret_version(name=resource).payload.data.decode("utf-8")


def _make_client(vendor: str, api_key_secret: str) -> AuditClient:
    api_key = _secret(api_key_secret)
    if vendor == "anthropic":
        return AnthropicComplianceClient(api_key)
    if vendor == "anthropic_chats":
        return AnthropicChatContentClient(api_key)
    if vendor == "openai":
        return OpenAIAuditClient(api_key)
    if vendor == "openai_conversations":
        # principal_id (workspace UUID or org id) sourced from
        # OPENAI_PRINCIPAL_ID env var; OPENAI_PRINCIPAL_SCOPE picks
        # workspaces (default) vs organizations.
        return OpenAIConversationsClient(api_key)
    raise ValueError(
        f"Unsupported VENDOR={vendor!r}. Supported: "
        "'anthropic', 'anthropic_chats', 'openai', 'openai_conversations'."
    )


@functions_framework.cloud_event
def handler(cloud_event):
    vendor = os.environ["VENDOR"]
    project = os.environ["GCP_PROJECT"]
    api_key_secret = os.environ["API_KEY_SECRET"]
    audit_topic = os.environ["AUDIT_TOPIC"]
    lookback = int(os.environ.get("INITIAL_LOOKBACK_MINUTES", "60"))

    client = _make_client(vendor, api_key_secret)
    egress = PubSubEgress(project=project, topic=audit_topic, vendor=vendor)
    store = FirestoreStateStore(vendor=vendor, project=project)

    summary = run(client, egress, store, initial_lookback_minutes=lookback)
    log.info("summary=%s", summary)
    return summary
