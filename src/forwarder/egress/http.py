"""HTTP Collector egress (fallback).

Direct POST to the XSIAM HTTP Log Collector at
`https://api-{tenant}.xdr.{region}.paloaltonetworks.com/logs/v1/event`.

Use this only when the cloud-native paths (S3+SQS / Pub/Sub) aren't viable.
The native paths are preferred because they buffer naturally, replay
easily, and match the cross-account IAM-role auditing pattern the SOC uses
elsewhere.

Per the public XSIAM HTTP Collector reference:
- ≤5 MB per event, ≤9.5 MB per batch (we cap conservatively at 4 MB).
- JSON body, `Content-Type: application/json`.
- Auth header name and gzip support are NOT publicly documented at the
  exact level needed; the defaults below align with Cribl's published XSIAM
  destination integration but should be verified against your tenant's
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
    vendor: str
    auth_header: str = "Authorization"
    product: str = "audit_log"  # vendor-prefixed at runtime
    use_gzip: bool = True


class HttpEgress:
    def __init__(
        self, config: HttpEgressConfig, http: urllib3.PoolManager | None = None
    ):
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
        # Vendors emit different time-field names; the adapters that produce
        # wrapped payloads (anthropic_chats / openai_conversations) put the
        # canonical timestamp inside .message.created_at or .message.effective_at.
        v = self._cfg.vendor
        if v == "anthropic":
            t = ev.get("created_at")
            ev_type = ev.get("type")
        elif v == "anthropic_chats":
            msg = ev.get("message") or {}
            t = msg.get("created_at")
            ev_type = "claude_chat_message"
        elif v == "openai":
            ts = ev.get("effective_at")
            t = _unix_to_iso(int(ts)) if isinstance(ts, (int, float)) else None
            ev_type = ev.get("type")
        elif v == "openai_conversations":
            # Wrapped payload: {file_id, list_entry, record}
            record = ev.get("record") or {}
            ts = (
                record.get("created_at")
                or record.get("timestamp")
                or record.get("effective_at")
            )
            if isinstance(ts, (int, float)):
                t = _unix_to_iso(int(ts))
            elif isinstance(ts, str):
                t = ts
            else:
                t = None
            list_entry = ev.get("list_entry") or {}
            ev_type = list_entry.get("event_type") or "openai_compliance_log"
        else:
            t = ev.get("created_at") or ev.get("effective_at")
            ev_type = ev.get("type")
        return {
            "_vendor": v,
            "_product": f"{v}_{self._cfg.product}",
            "_time": t,
            "_event": ev_type,
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


def _unix_to_iso(ts: int) -> str:
    from datetime import datetime, timezone

    return (
        datetime.fromtimestamp(int(ts), tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )
