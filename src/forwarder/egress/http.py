"""HTTP Collector egress (fallback).

Direct POST to the XSIAM HTTP Log Collector at
`https://api-{tenant}.xdr.{region}.paloaltonetworks.com/logs/v1/event`.

Use this only when the cloud-native paths (S3+SQS / Pub/Sub) aren't viable.
The native paths are preferred because they buffer naturally, replay easily,
and match the cross-account IAM-role auditing pattern the SOC uses elsewhere.

Per the public XSIAM HTTP Collector reference:
- ≤5 MB per event, ≤9.5 MB per batch (we cap conservatively at 4 MB).
- JSON body, `Content-Type: application/json`.
- Auth header name and gzip support are NOT publicly documented at the
  exact level needed; the defaults below are aligned with Cribl's published
  XSIAM destination integration but should be verified against your tenant's
  HTTP Collector configuration screen before production.
"""

from __future__ import annotations

import gzip
import json
import logging
import time
from dataclasses import dataclass
from typing import Iterable

import urllib3

log = logging.getLogger(__name__)

MAX_BATCH_BYTES = 4 * 1024 * 1024


@dataclass
class HttpEgressConfig:
    url: str
    token: str
    auth_header: str = "Authorization"  # TODO(verify): some collectors use x-xdr-auth-id pair
    vendor: str = "anthropic"
    product: str = "claude_compliance_audit"
    use_gzip: bool = True


class HttpEgress:
    def __init__(self, config: HttpEgressConfig, http: urllib3.PoolManager | None = None):
        self._cfg = config
        self._http = http or urllib3.PoolManager(retries=False, timeout=30.0)

    def send(self, events: Iterable[dict]) -> int:
        batch: list[dict] = []
        batch_bytes = 0
        sent = 0

        for ev in events:
            enriched = self._enrich(ev)
            line_bytes = len(json.dumps(enriched, separators=(",", ":")))
            if batch and batch_bytes + line_bytes > MAX_BATCH_BYTES:
                self._post(batch)
                sent += len(batch)
                batch = []
                batch_bytes = 0
            batch.append(enriched)
            batch_bytes += line_bytes

        if batch:
            self._post(batch)
            sent += len(batch)
        return sent

    def _enrich(self, ev: dict) -> dict:
        return {
            "_vendor": self._cfg.vendor,
            "_product": self._cfg.product,
            "_time": ev.get("created_at"),
            "event": ev,
        }

    def _post(self, batch: list[dict], attempts: int = 4) -> None:
        raw = json.dumps(batch, separators=(",", ":")).encode("utf-8")
        headers = {
            self._cfg.auth_header: self._cfg.token,
            "Content-Type": "application/json",
        }
        if self._cfg.use_gzip:
            body = gzip.compress(raw)
            headers["Content-Encoding"] = "gzip"
        else:
            body = raw

        backoff = 1.0
        for i in range(attempts):
            r = self._http.request("POST", self._cfg.url, body=body, headers=headers)
            if r.status in (200, 202):
                log.info("XSIAM HTTP Collector accepted %d events", len(batch))
                return
            if r.status == 429 or 500 <= r.status < 600:
                if i == attempts - 1:
                    raise RuntimeError(
                        f"XSIAM rejected batch after {attempts} attempts: "
                        f"HTTP {r.status} {r.data[:200]!r}"
                    )
                log.warning("XSIAM HTTP %s, retrying in %.1fs", r.status, backoff)
                time.sleep(backoff)
                backoff *= 2
                continue
            raise RuntimeError(f"XSIAM HTTP {r.status}: {r.data[:500]!r}")
