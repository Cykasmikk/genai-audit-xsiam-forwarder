"""GCP Cloud Function (Gen 2) handler. Native ingest path: Pub/Sub → XSIAM."""

from __future__ import annotations

import logging
import os

import functions_framework
from google.cloud import secretmanager

from .claude_client import ClaudeComplianceClient
from .core import run
from .egress.pubsub import PubSubEgress
from .state_gcp import FirestoreStateStore

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


def _secret(resource: str) -> str:
    client = secretmanager.SecretManagerServiceClient()
    return client.access_secret_version(name=resource).payload.data.decode("utf-8")


@functions_framework.cloud_event
def handler(cloud_event):
    project = os.environ["GCP_PROJECT"]
    anthropic_secret = os.environ["ANTHROPIC_KEY_SECRET"]
    audit_topic = os.environ["AUDIT_TOPIC"]
    lookback = int(os.environ.get("INITIAL_LOOKBACK_MINUTES", "60"))

    claude = ClaudeComplianceClient(_secret(anthropic_secret))
    egress = PubSubEgress(project=project, topic=audit_topic)
    store = FirestoreStateStore(project=project)

    summary = run(claude, egress, store, initial_lookback_minutes=lookback)
    log.info("summary=%s", summary)
    return summary
