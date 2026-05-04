"""Anthropic Claude Compliance API client.

The Compliance API is the documented source for Claude Platform audit events
on Enterprise plans. It is *not* the Usage/Cost API.

Key references:
  - Overview:  https://claude.com/blog/claude-platform-compliance-api
  - Enable:    https://support.claude.com/en/articles/13015708-access-the-compliance-api
  - Audit log
    schema:    https://support.claude.com/en/articles/9970975-access-audit-logs

PUBLIC DOCS DO NOT DISCLOSE the exact endpoint path or pagination parameter
names — those are in a PDF spec issued through the Anthropic Trust Center to
customers with the Compliance API enabled. The constants below are educated
guesses aligned with the sibling Usage/Cost Admin API conventions and are
flagged with TODO(compliance-pdf). Override at deploy time via env var if your
PDF says otherwise:

    COMPLIANCE_API_PATH=/v1/organizations/<actual_path_from_pdf>

The client makes no other assumptions about the wire format beyond what is
publicly documented:
  - `x-api-key` header carries an Admin API key (`sk-ant-admin01-...`)
  - `anthropic-version: 2023-06-01` header is required
  - Time filter uses ISO 8601 timestamps
  - Event payload contains the documented fields:
        created_at, actor_info, event, event_info, entity_info,
        ip_address, device_id, user_agent, client_platform
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterator
from urllib.parse import urlencode

import urllib3

log = logging.getLogger(__name__)

ANTHROPIC_API_BASE = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"

# TODO(compliance-pdf): verify against the Compliance API PDF from the Anthropic
# Trust Center. The path is not disclosed in any public Anthropic page indexed
# at the time of writing. Set COMPLIANCE_API_PATH env var to override.
COMPLIANCE_API_PATH = os.environ.get(
    "COMPLIANCE_API_PATH", "/v1/organizations/audit_logs"
)

# TODO(compliance-pdf): verify pagination param names. The sibling Usage/Cost
# Admin API uses `page` (token) + `has_more` + `next_page` + `limit`. We assume
# Compliance API follows the same convention.
PARAM_LIMIT = "limit"
PARAM_PAGE = "page"
PARAM_STARTING_AT = "starting_at"
PARAM_ENDING_AT = "ending_at"
RESP_HAS_MORE = "has_more"
RESP_NEXT_PAGE = "next_page"
RESP_DATA = "data"

PAGE_LIMIT = 1000


@dataclass
class AuditEvent:
    """Audit event using the publicly documented schema.

    Field names match the support.claude.com/articles/9970975-access-audit-logs
    listing. There is no documented top-level event-id field, so we dedupe by
    a content hash and watermark by `created_at`.
    """

    created_at: str
    event: str
    actor_info: dict = field(default_factory=dict)
    event_info: dict = field(default_factory=dict)
    entity_info: dict = field(default_factory=dict)
    ip_address: str | None = None
    device_id: str | None = None
    user_agent: str | None = None
    client_platform: str | None = None
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: dict) -> "AuditEvent":
        return cls(
            created_at=payload["created_at"],
            event=payload.get("event", ""),
            actor_info=payload.get("actor_info") or {},
            event_info=payload.get("event_info") or {},
            entity_info=payload.get("entity_info") or {},
            ip_address=payload.get("ip_address"),
            device_id=payload.get("device_id"),
            user_agent=payload.get("user_agent"),
            client_platform=payload.get("client_platform"),
            raw=payload,
        )

    @property
    def created_at_dt(self) -> datetime:
        return datetime.fromisoformat(self.created_at.replace("Z", "+00:00"))

    @property
    def content_hash(self) -> str:
        """Stable hash for dedupe across overlapping fetch windows."""
        canonical = json.dumps(self.raw, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ComplianceAPIError(RuntimeError):
    """Raised on non-retriable Compliance API responses."""


class ClaudeComplianceClient:
    def __init__(
        self,
        admin_api_key: str,
        api_base: str = ANTHROPIC_API_BASE,
        api_path: str = COMPLIANCE_API_PATH,
        http: urllib3.PoolManager | None = None,
    ):
        if not admin_api_key.startswith("sk-ant-admin"):
            raise ValueError(
                "Compliance API requires an Admin API key (sk-ant-admin01-...). "
                "Compliance access keys are issued via Org settings → Data and "
                "Privacy after enabling the Compliance API."
            )
        self._key = admin_api_key
        self._base = api_base.rstrip("/")
        self._path = api_path
        self._http = http or urllib3.PoolManager(retries=False, timeout=30.0)

    def _headers(self) -> dict:
        return {
            "x-api-key": self._key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
            "user-agent": "claude-xsiam-forwarder/1.0",
        }

    def fetch_window(
        self,
        starting_at: datetime,
        ending_at: datetime,
    ) -> Iterator[AuditEvent]:
        """Yield audit events whose `created_at` falls in [starting_at, ending_at).

        Yielded oldest-first so callers can advance a watermark monotonically.
        Pages through the documented sibling-API pagination scheme; if the
        Compliance API uses a different scheme, override the PARAM_* / RESP_*
        constants or supply your own client.
        """
        params: dict = {
            PARAM_LIMIT: PAGE_LIMIT,
            PARAM_STARTING_AT: starting_at.astimezone(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            PARAM_ENDING_AT: ending_at.astimezone(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
        }
        next_page: str | None = None

        page_buffer: list[AuditEvent] = []
        page_count = 0

        while True:
            if next_page:
                params[PARAM_PAGE] = next_page
            url = f"{self._base}{self._path}?{urlencode(params)}"
            payload = self._request_with_retry(url)

            data = payload.get(RESP_DATA, [])
            for raw in data:
                page_buffer.append(AuditEvent.from_payload(raw))

            page_count += 1
            if not payload.get(RESP_HAS_MORE):
                break
            next_page = payload.get(RESP_NEXT_PAGE)
            if not next_page:
                # has_more=true but no next_page token — defensive break.
                log.warning("Compliance API returned has_more without next_page")
                break

        log.info(
            "fetched window starting_at=%s ending_at=%s pages=%d events=%d",
            params[PARAM_STARTING_AT],
            params[PARAM_ENDING_AT],
            page_count,
            len(page_buffer),
        )

        # Sort oldest-first regardless of API order, so the watermark always
        # advances monotonically and a mid-batch crash resumes correctly.
        page_buffer.sort(key=lambda e: e.created_at)
        for ev in page_buffer:
            yield ev

    def _request_with_retry(self, url: str, attempts: int = 4) -> dict:
        backoff = 1.0
        for i in range(attempts):
            r = self._http.request("GET", url, headers=self._headers())
            if r.status == 404:
                raise ComplianceAPIError(
                    f"Compliance API path not found: {self._path}. "
                    "Verify against the Compliance API PDF from the Anthropic "
                    "Trust Center and override via the COMPLIANCE_API_PATH env "
                    f"var. Server response: {r.data[:200]!r}"
                )
            if r.status in (401, 403):
                raise ComplianceAPIError(
                    f"Compliance API auth rejected (HTTP {r.status}). Check that "
                    "(a) the Compliance API is enabled for your organization "
                    "(Org settings → Data and Privacy), and (b) the Admin API "
                    f"key has compliance scope. Response: {r.data[:200]!r}"
                )
            if r.status == 429 or 500 <= r.status < 600:
                if i == attempts - 1:
                    raise ComplianceAPIError(
                        f"Compliance API failed after {attempts} attempts: "
                        f"HTTP {r.status} {r.data[:200]!r}"
                    )
                log.warning(
                    "Compliance API HTTP %s, retrying in %.1fs", r.status, backoff
                )
                time.sleep(backoff)
                backoff *= 2
                continue
            if r.status >= 400:
                raise ComplianceAPIError(
                    f"Compliance API HTTP {r.status}: {r.data[:500]!r}"
                )
            return json.loads(r.data)
        raise ComplianceAPIError("unreachable")
