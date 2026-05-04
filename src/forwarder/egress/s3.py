"""S3 egress: writes audit events as gzipped JSON-lines to S3.

XSIAM's "Amazon S3 generic logs" data source pulls objects from S3 driven
by SQS ObjectCreated notifications. Reference Palo-published architecture:
https://github.com/PaloAltoNetworks/terraform-umbrella-s3-to-xsiam-ingestion-module

Object layout
-------------
    s3://{bucket}/{prefix}/{yyyy}/{mm}/{dd}/{hh}/{run_id}.jsonl.gz

One Lambda invocation produces at most one object. Hour partitioning matches
the convention XSIAM expects for time-window scans and matches CloudTrail/
GuardDuty/ALB log layouts already in the SOC.

Format
------
- Newline-delimited JSON, one event per line (the raw Compliance API payload).
- gzip compressed (`Content-Encoding: gzip`, `Content-Type: application/x-ndjson`).
- Server-side encrypted (the bucket's default policy enforces this; we set
  `ServerSideEncryption=AES256` defensively in case the bucket policy lapses).
"""

from __future__ import annotations

import gzip
import io
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Iterable

log = logging.getLogger(__name__)


class S3Egress:
    def __init__(
        self,
        bucket: str,
        prefix: str = "claude-compliance",
        s3_client=None,
    ):
        self._bucket = bucket
        self._prefix = prefix.strip("/")
        if s3_client is not None:
            self._s3 = s3_client
        else:
            import boto3  # deferred so the module imports without boto3
            self._s3 = boto3.client("s3")

    def send(self, events: Iterable[dict]) -> int:
        # Materialize once: we need the count and a stable byte stream, and
        # audit-log batches are small enough (≤ thousands per run) that the
        # memory cost is negligible.
        materialized = list(events)
        if not materialized:
            return 0

        body = self._serialize(materialized)
        key = self._object_key()

        self._s3.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=body,
            ContentType="application/x-ndjson",
            ContentEncoding="gzip",
            ServerSideEncryption="AES256",
        )
        log.info(
            "wrote %d events to s3://%s/%s (%d bytes gzipped)",
            len(materialized),
            self._bucket,
            key,
            len(body),
        )
        return len(materialized)

    def _serialize(self, events: list[dict]) -> bytes:
        buf = io.BytesIO()
        with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
            for ev in events:
                gz.write(json.dumps(ev, separators=(",", ":")).encode("utf-8"))
                gz.write(b"\n")
        return buf.getvalue()

    def _object_key(self) -> str:
        now = datetime.now(timezone.utc)
        run_id = uuid.uuid4().hex[:12]
        return (
            f"{self._prefix}/"
            f"{now:%Y/%m/%d/%H}/"
            f"{now:%Y%m%dT%H%M%SZ}-{run_id}.jsonl.gz"
        )
