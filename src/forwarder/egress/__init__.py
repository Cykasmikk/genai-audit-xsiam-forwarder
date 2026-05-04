"""Egress sinks for forwarded audit events.

Three implementations:
- `S3Egress`    — writes gzipped JSON-lines to S3; XSIAM pulls via SQS notify.
                  This is the documented native pattern for AWS.
- `PubSubEgress`— publishes to a Pub/Sub topic; XSIAM pulls via subscription.
                  This is the documented native pattern for GCP.
- `HttpEgress`  — POSTs directly to the XSIAM HTTP Collector. Fallback only;
                  use when S3/Pub-Sub are unavailable.

Pick the right sink for your deployment in `aws_handler.py` / `gcp_handler.py`.
"""

from __future__ import annotations

from typing import Iterable, Protocol


class Egress(Protocol):
    def send(self, events: Iterable[dict]) -> int:
        """Forward events to the destination. Returns count successfully sent.

        Implementations MUST raise on partial failure so the caller can avoid
        advancing the watermark past unsent events.
        """
        ...
