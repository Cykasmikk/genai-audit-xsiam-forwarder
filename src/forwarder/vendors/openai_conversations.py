"""OpenAI Compliance Logs Platform — conversations & audit-log file adapter.

Conforms to the spec published at:
  https://developers.openai.com/cookbook/examples/chatgpt/compliance_api/logs_platform

Architecture difference from the Audit Logs API
----------------------------------------------
The Compliance Logs Platform is **not** an event stream like
`/v1/organization/audit_logs`. It's a two-stage delivery:

  1. List the JSONL log *files* available for a workspace (or org) in a
     time window:
        GET https://api.chatgpt.com/v1/compliance/{scope}/{principal_id}/logs
            ?limit=…&after=ISO8601_ts&event_type=…
        Response: { data: [{id, ...}, ...], has_more, last_end_time }

  2. For each log-file id, download the JSONL content (signed-URL
     redirect, follow with -L):
        GET https://api.chatgpt.com/v1/compliance/{scope}/{principal_id}/logs/{id}
        Response: JSONL bytes — one event per line.

Pagination over file listings: feed `last_end_time` from each response
back as `after` until `has_more=false`. Inside each downloaded file,
parse line by line.

Auth
----
`Authorization: Bearer <COMPLIANCE_API_KEY>` — note this is a separate
key type from the Admin API key (`sk-admin-`). The Compliance API key
is provisioned via the Enterprise/Edu Compliance Platform onboarding
flow; its prefix is not publicly documented, so we don't enforce a
specific prefix — we just require non-empty.

Required configuration
----------------------
- `OPENAI_COMPLIANCE_API_KEY` — the Compliance API key
- `OPENAI_PRINCIPAL_ID` — workspace UUID (or `org-…` if `OPENAI_PRINCIPAL_SCOPE=organizations`)
- `OPENAI_PRINCIPAL_SCOPE` (default `workspaces`) — `workspaces` or
  `organizations`

The adapter **fails closed at construction time** if `principal_id` is
missing — there is no sensible default that would yield real data.

Vendor key
----------
`openai_conversations` — namespaced separately from `openai` (Audit
Logs). State, S3 prefix, Pub/Sub topic, and XSIAM data source are all
distinct.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Iterator
from urllib.parse import urlencode

import urllib3

from . import AuditEvent

log = logging.getLogger(__name__)

VENDOR = "openai_conversations"

OPENAI_COMPLIANCE_API_BASE = os.environ.get(
    "OPENAI_COMPLIANCE_API_BASE", "https://api.chatgpt.com"
)
# Path template — substitute {scope} and {principal_id} at runtime.
LOGS_PATH_TEMPLATE = os.environ.get(
    "OPENAI_COMPLIANCE_LOGS_PATH_TEMPLATE",
    "/v1/compliance/{scope}/{principal_id}/logs",
)

# Optional event-type filter. When unset, the API returns all event
# categories the workspace has logging enabled for — recommended for
# full-take SOC ingestion.
DEFAULT_EVENT_TYPE = os.environ.get("OPENAI_EVENT_TYPE")

PARAM_LIMIT = "limit"
PARAM_AFTER = "after"
PARAM_EVENT_TYPE = "event_type"
RESP_DATA = "data"
RESP_HAS_MORE = "has_more"
RESP_LAST_END_TIME = "last_end_time"

# Page size for the file-listing call. The cookbook doesn't specify a
# max; 100 mirrors the Audit Logs API cap and is conservative.
LIST_PAGE_LIMIT = 100

VALID_SCOPES = ("workspaces", "organizations")


class OpenAIConversationsAPIError(RuntimeError):
    """Raised on non-retriable Compliance Logs Platform responses."""


class OpenAIConversationsClient:
    vendor = VENDOR

    def __init__(
        self,
        api_key: str,
        principal_id: str | None = None,
        scope: str = "workspaces",
        event_type: str | None = None,
        api_base: str = OPENAI_COMPLIANCE_API_BASE,
        path_template: str = LOGS_PATH_TEMPLATE,
        http: urllib3.PoolManager | None = None,
    ):
        if not api_key:
            raise ValueError(
                "OpenAI Compliance Logs Platform requires a Compliance API key. "
                "Provision via your Enterprise/Edu Compliance Platform "
                "onboarding flow — this is distinct from the Admin API key "
                "(sk-admin-) used for /v1/organization/audit_logs."
            )

        principal_id = principal_id or os.environ.get("OPENAI_PRINCIPAL_ID")
        if not principal_id:
            raise ValueError(
                "OpenAI Compliance Logs Platform requires OPENAI_PRINCIPAL_ID "
                "(workspace UUID, or org-… id if scope=organizations). "
                "There is no useful default — refusing to start."
            )

        scope = scope or os.environ.get("OPENAI_PRINCIPAL_SCOPE") or "workspaces"
        if scope not in VALID_SCOPES:
            raise ValueError(
                f"OPENAI_PRINCIPAL_SCOPE must be one of {VALID_SCOPES}, got {scope!r}"
            )

        self._key = api_key
        self._principal_id = principal_id
        self._scope = scope
        self._event_type = event_type if event_type is not None else DEFAULT_EVENT_TYPE
        self._base = api_base.rstrip("/")
        self._path = path_template.format(scope=scope, principal_id=principal_id)
        self._http = http or urllib3.PoolManager(retries=False, timeout=60.0)

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/json",
            "User-Agent": "genai-audit-xsiam-forwarder/2.0",
        }

    def fetch_window(
        self,
        starting_at: datetime,
        ending_at: datetime,
    ) -> Iterator[AuditEvent]:
        """Two-stage retrieval: list log files, then download each.

        Each downloaded JSONL file contains one event per line; we yield
        one `AuditEvent` per line. Files whose `last_end_time` falls
        outside `[starting_at, ending_at]` are skipped client-side
        (the API's `after` filter is half-open and we want a closed
        window for our overlap-dedupe model).
        """
        list_url = f"{self._base}{self._path}"
        after_ts = _iso_z(starting_at)
        ending_at_iso = _iso_z(ending_at)
        page_count = 0
        files_collected = 0
        events_yielded = 0

        while True:
            params: dict = {
                PARAM_LIMIT: LIST_PAGE_LIMIT,
                PARAM_AFTER: after_ts,
            }
            if self._event_type:
                params[PARAM_EVENT_TYPE] = self._event_type
            payload = self._request_json(f"{list_url}?{urlencode(params)}")

            page_count += 1
            data = payload.get(RESP_DATA, []) or []
            for entry in data:
                file_id = entry.get("id")
                if not file_id:
                    log.warning(
                        "openai_conversations: log-file listing entry missing id; skipping"
                    )
                    continue
                # Skip files whose end_time is past our window upper bound
                file_end = entry.get("end_time") or entry.get("last_end_time")
                if isinstance(file_end, str) and file_end > ending_at_iso:
                    continue
                files_collected += 1
                yield from self._download_and_parse(file_id, entry)
                events_yielded += 1  # ≥1 per file usually; loop counts them

            if not payload.get(RESP_HAS_MORE):
                break
            next_after = payload.get(RESP_LAST_END_TIME)
            if not next_after or next_after == after_ts:
                log.warning(
                    "openai_conversations: has_more=true but last_end_time missing/unchanged"
                )
                break
            # Stop early if the cursor crosses our window upper bound.
            if next_after > ending_at_iso:
                break
            after_ts = next_after

        log.info(
            "openai_conversations: scope=%s principal=%s pages=%d files=%d",
            self._scope,
            self._principal_id,
            page_count,
            files_collected,
        )

    def _download_and_parse(
        self, file_id: str, list_entry: dict
    ) -> Iterator[AuditEvent]:
        """Download a JSONL log file and yield one AuditEvent per line."""
        url = f"{self._base}{self._path}/{file_id}"
        body = self._request_bytes(url)
        # JSONL — one JSON object per line. Tolerate trailing newlines and
        # empty lines defensively.
        for raw_line in body.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as e:
                log.warning(
                    "openai_conversations: malformed JSONL line in file %s: %s",
                    file_id,
                    e,
                )
                continue
            yield _to_event(rec, file_id, list_entry)

    def _request_json(self, url: str, attempts: int = 4) -> dict:
        body = self._request_with_retry(url, attempts)
        try:
            return json.loads(body)
        except json.JSONDecodeError as e:
            raise OpenAIConversationsAPIError(
                f"OpenAI Compliance Logs returned non-JSON list response from "
                f"{url}: {body[:200]!r} — {e}"
            )

    def _request_bytes(self, url: str, attempts: int = 4) -> bytes:
        # The cookbook uses curl -L (follow redirects). urllib3 follows
        # redirects by default, so a normal GET works.
        return self._request_with_retry(url, attempts)

    def _request_with_retry(self, url: str, attempts: int) -> bytes:
        backoff = 1.0
        for i in range(attempts):
            r = self._http.request("GET", url, headers=self._headers())
            if r.status == 404:
                raise OpenAIConversationsAPIError(
                    f"OpenAI Compliance Logs path not found: {url}. "
                    "Per the cookbook, the path is "
                    "/v1/compliance/{scope}/{principal_id}/logs at host "
                    "api.chatgpt.com (NOT api.openai.com). Override via "
                    "OPENAI_COMPLIANCE_API_BASE / "
                    "OPENAI_COMPLIANCE_LOGS_PATH_TEMPLATE if a newer "
                    f"revision moved it. Server response: {r.data[:200]!r}"
                )
            if r.status in (401, 403):
                raise OpenAIConversationsAPIError(
                    f"OpenAI Compliance Logs auth rejected (HTTP {r.status}). "
                    "Verify: (a) you have a Compliance API key (not just an "
                    "Admin key — these are different), (b) the workspace ID "
                    "/ org id matches the key's scope, (c) the Compliance "
                    f"Platform is enabled for your tenant. Response: {r.data[:200]!r}"
                )
            if r.status == 400:
                raise OpenAIConversationsAPIError(
                    f"OpenAI Compliance Logs rejected request (HTTP 400): "
                    f"{r.data[:500]!r}"
                )
            if r.status == 429 or 500 <= r.status < 600:
                if i == attempts - 1:
                    raise OpenAIConversationsAPIError(
                        f"OpenAI Compliance Logs failed after {attempts} attempts: "
                        f"HTTP {r.status} {r.data[:200]!r}"
                    )
                log.warning(
                    "openai_conversations HTTP %s, retrying in %.1fs",
                    r.status,
                    backoff,
                )
                time.sleep(backoff)
                backoff *= 2
                continue
            if r.status >= 400:
                raise OpenAIConversationsAPIError(
                    f"OpenAI Compliance Logs HTTP {r.status}: {r.data[:500]!r}"
                )
            return r.data
        raise OpenAIConversationsAPIError("unreachable")


def _to_event(rec: dict, file_id: str, list_entry: dict) -> AuditEvent:
    """Normalize a JSONL record to the common AuditEvent shape.

    Records have varying schemas by event_type. We extract `id` and a
    timestamp where possible; both are heuristic across the documented
    log categories (auth, audit, conversations, codex, files,
    workspace_users, memories).
    """
    rec_id = (
        rec.get("id")
        or rec.get("event_id")
        or rec.get("conversation_id")
        or rec.get("message_id")
        or _synthetic_id(rec, file_id)
    )

    ts = (
        rec.get("created_at")
        or rec.get("timestamp")
        or rec.get("effective_at")
        or rec.get("event_time")
        or list_entry.get("end_time")
        or list_entry.get("last_end_time")
    )
    if isinstance(ts, (int, float)):
        # Some categories use Unix seconds; others ISO 8601.
        created_at = (
            datetime.fromtimestamp(int(ts), tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
    elif isinstance(ts, str):
        created_at = ts if ts.endswith("Z") else ts.replace("+00:00", "Z")
    else:
        created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    payload = {"file_id": file_id, "list_entry": list_entry, "record": rec}
    return AuditEvent(id=str(rec_id), created_at=created_at, vendor=VENDOR, raw=payload)


def _synthetic_id(rec: dict, file_id: str) -> str:
    import hashlib

    canonical = json.dumps(rec, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]
    return f"synthetic_{file_id}_{digest}"


def _iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
