"""DynamoDB-backed forwarder state store."""

from __future__ import annotations

import boto3

from .state import ForwarderState

STATE_PK = "claude_compliance_state"


class DynamoStateStore:
    def __init__(self, table_name: str, region: str | None = None):
        self._table = boto3.resource("dynamodb", region_name=region).Table(table_name)

    def load(self) -> ForwarderState:
        resp = self._table.get_item(Key={"pk": STATE_PK})
        item = resp.get("Item")
        if not item:
            return ForwarderState()
        return ForwarderState(
            watermark=item.get("watermark"),
            recent_hashes=list(item.get("recent_hashes") or []),
        )

    def save(self, state: ForwarderState) -> None:
        self._table.put_item(
            Item={
                "pk": STATE_PK,
                "watermark": state.watermark,
                "recent_hashes": state.recent_hashes,
            }
        )
