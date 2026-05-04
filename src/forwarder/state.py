"""Forwarder state backend protocol.

The Compliance API does not document a stable event-id field, so we cannot
cursor by id. Instead we persist:

  - `watermark`: ISO 8601 timestamp of the latest event we've forwarded
  - `recent_hashes`: SHA-256 content hashes of events near the watermark,
    used to dedupe the inevitable overlap when the next poll re-queries the
    boundary window to handle clock skew and late-arriving events.

The state document is small (a string + a bounded list), well under DynamoDB
and Firestore item limits.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class ForwarderState:
    watermark: str | None = None  # ISO 8601 created_at of newest forwarded event
    recent_hashes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"watermark": self.watermark, "recent_hashes": self.recent_hashes}

    @classmethod
    def from_dict(cls, d: dict | None) -> "ForwarderState":
        if not d:
            return cls()
        return cls(
            watermark=d.get("watermark"),
            recent_hashes=list(d.get("recent_hashes") or []),
        )


class StateStore(Protocol):
    def load(self) -> ForwarderState: ...
    def save(self, state: ForwarderState) -> None: ...
