"""DynamoDB-backed forwarder state store, namespaced by vendor."""

from __future__ import annotations

import boto3

from .state import ForwarderState

# Legacy single-vendor pk used by initial deploys (Anthropic-only). The
# Anthropic store reads this as a fallback so an in-place upgrade preserves
# its dedupe history.
_LEGACY_ANTHROPIC_PK = "claude_compliance_state"


class DynamoStateStore:
    def __init__(self, vendor: str, table_name: str, region: str | None = None):
        self.vendor = vendor
        self._pk = f"{vendor}_audit_state"
        self._table = boto3.resource("dynamodb", region_name=region).Table(table_name)

    def load(self) -> ForwarderState:
        item = self._table.get_item(Key={"pk": self._pk}).get("Item")
        if not item and self.vendor == "anthropic":
            item = self._table.get_item(Key={"pk": _LEGACY_ANTHROPIC_PK}).get("Item")
        if not item:
            return ForwarderState()
        return ForwarderState(
            watermark=item.get("watermark"),
            recent_ids=list(item.get("recent_ids") or item.get("recent_hashes") or []),
        )

    def save(self, state: ForwarderState) -> None:
        self._table.put_item(
            Item={
                "pk": self._pk,
                "watermark": state.watermark,
                "recent_ids": state.recent_ids,
            }
        )
