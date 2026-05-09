"""OpenAI Compliance Logs Platform — conversations adapter (skeleton).

⚠️  PARTIAL SPEC ⚠️

OpenAI's Compliance Platform conversation logs endpoint is not publicly
documented as of the latest indexed search results — it's gated behind
Enterprise/Edu access. The constants below are educated defaults aligned
with OpenAI's sibling Audit Logs API conventions (Authorization Bearer,
effective_at[gte]/[lte] bracket-notation Unix seconds, after/before
cursor) and are flagged with TODO(openai-conversations-spec).

To productionize:
  1. Get the authoritative spec from OpenAI Enterprise support, OR
  2. Use Palo Alto Networks' native XSIAM "OpenAI ChatGPT Enterprise
     Compliance" integration if your tenant has access (announced 2026
     per palo's community blog) — that may make this adapter unnecessary.
  3. Override constants via env var if the path / params differ.

What is publicly known (March 2026 release notes):
  - The new conversations system delivers "immutable, time-windowed JSONL
    log files" with "minutes-level latency".
  - Replaces the old stateful route which is removed June 5, 2026.
  - Auth is OpenAI Admin key (sk-admin-) per the broader Compliance
    Logs Platform.
  - Polling cadence ~1 hour per Sumo Logic's published integration docs
    (we still default to 5 min — adjust if rate-limited).
  - Workspace ID is required for the configuration on the customer side.

Vendor key
----------
This adapter publishes under **`openai_conversations`** (distinct from
`openai` which is the Audit Logs adapter). State, S3 prefix, Pub/Sub
topic, and XSIAM data source are all separate.
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

OPENAI_API_BASE = "https://api.openai.com"

# TODO(openai-conversations-spec): verify the exact path. Current best guess
# is /v1/organization/conversations to mirror /v1/organization/audit_logs.
# Some sources hint at /v1/conversations or a /compliance/-prefixed path;
# override via OPENAI_CONVERSATIONS_PATH if your spec differs.
CONVERSATIONS_PATH = os.environ.get(
    "OPENAI_CONVERSATIONS_PATH", "/v1/organization/conversations"
)

# Workspace scoping: the Sumo Logic config requires a workspace ID; we
# pass it as a query parameter or path segment depending on the real spec.
# Override path template if it's per-workspace, e.g.:
#   /v1/organization/workspaces/{workspace_id}/conversations
OPENAI_WORKSPACE_ID = os.environ.get("OPENAI_WORKSPACE_ID")

PARAM_LIMIT = "limit"
PARAM_AFTER = "after"
PARAM_EFFECTIVE_AT_GTE = "effective_at[gte]"
PARAM_EFFECTIVE_AT_LTE = "effective_at[lte]"
PARAM_WORKSPACE_ID = "workspace_id"
RESP_DATA = "data"
RESP_HAS_MORE = "has_more"
RESP_LAST_ID = "last_id"

# TODO(openai-conversations-spec): confirm against real spec. OpenAI Audit
# Logs caps at 100; conversations may be different.
PAGE_LIMIT = 100

_VALID_KEY_PREFIX = "sk-admin-"


class OpenAIConversationsAPIError(RuntimeError):
    pass


class OpenAIConversationsClient:
    vendor = VENDOR

    def __init__(
        self,
        api_key: str,
        api_base: str = OPENAI_API_BASE,
        api_path: str = CONVERSATIONS_PATH,
        workspace_id: str | None = None,
        http: urllib3.PoolManager | None = None,
    ):
        if not api_key.startswith(_VALID_KEY_PREFIX):
            raise ValueError(
                "OpenAI Conversations API requires an Admin key (sk-admin-...) "
                "issued via Platform dashboard → Admin keys. Standard project "
                "keys cannot read compliance conversation logs."
            )
        self._key = api_key
        self._base = api_base.rstrip("/")
        self._path = api_path
        self._workspace_id = workspace_id or OPENAI_WORKSPACE_ID
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
        """Yield AuditEvents for conversations updated in the window.

        TODO(openai-conversations-spec): the real spec may emit one
        record per CONVERSATION (with embedded messages) or one record
        per MESSAGE. This skeleton assumes per-message granularity for
        SOC ingest — if your spec returns conversation-level records,
        adapt the loop to iterate `payload["messages"]` and yield one
        AuditEvent per message.
        """
        base_params: dict = {
            PARAM_LIMIT: PAGE_LIMIT,
            PARAM_EFFECTIVE_AT_GTE: _to_unix(starting_at),
            PARAM_EFFECTIVE_AT_LTE: _to_unix(ending_at),
        }
        if self._workspace_id:
            base_params[PARAM_WORKSPACE_ID] = self._workspace_id

        after: str | None = None
        accumulated: list[AuditEvent] = []
        page_count = 0

        while True:
            params = dict(base_params)
            if after:
                params[PARAM_AFTER] = after
            url = f"{self._base}{self._path}?{urlencode(params)}"

            payload = self._request_with_retry(url)
            for raw in payload.get(RESP_DATA, []):
                # Defensive: handle either per-message or per-conversation
                # response shape until the spec is locked.
                if "messages" in raw and isinstance(raw["messages"], list):
                    convo_meta = {k: v for k, v in raw.items() if k != "messages"}
                    for msg in raw["messages"]:
                        accumulated.append(_to_event(msg, convo_meta))
                else:
                    accumulated.append(_to_event(raw, {}))

            page_count += 1
            if not payload.get(RESP_HAS_MORE):
                break
            next_cursor = payload.get(RESP_LAST_ID)
            if not next_cursor or next_cursor == after:
                log.warning(
                    "openai_conversations: has_more=true but last_id missing/unchanged"
                )
                break
            after = next_cursor

        log.info(
            "openai_conversations: fetched [%d, %d] pages=%d events=%d",
            base_params[PARAM_EFFECTIVE_AT_GTE],
            base_params[PARAM_EFFECTIVE_AT_LTE],
            page_count,
            len(accumulated),
        )

        accumulated.sort(key=lambda e: (e.created_at, e.id))
        for ev in accumulated:
            yield ev

    def _request_with_retry(self, url: str, attempts: int = 4) -> dict:
        backoff = 1.0
        for i in range(attempts):
            r = self._http.request("GET", url, headers=self._headers())
            if r.status == 404:
                raise OpenAIConversationsAPIError(
                    f"OpenAI Conversations path not found: {self._path}. "
                    "This adapter ships with educated defaults; the real "
                    "spec is gated behind OpenAI Enterprise. Override via "
                    "OPENAI_CONVERSATIONS_PATH env var with the path from "
                    "your OpenAI rep, OR consider Palo Alto Networks' "
                    "native XSIAM 'OpenAI ChatGPT Enterprise Compliance' "
                    f"integration if available. Response: {r.data[:200]!r}"
                )
            if r.status in (401, 403):
                raise OpenAIConversationsAPIError(
                    f"OpenAI Conversations auth rejected (HTTP {r.status}). "
                    "Verify: (a) Compliance Logs Platform is enabled for "
                    "your org, (b) the key starts with sk-admin- and was "
                    "issued by an Org Owner, (c) the key has the conversations "
                    f"scope. Response: {r.data[:200]!r}"
                )
            if r.status == 400:
                raise OpenAIConversationsAPIError(
                    f"OpenAI Conversations rejected request (HTTP 400). "
                    "If the message references unknown query params, the "
                    "spec we're querying with may differ from your tenant's. "
                    f"Response: {r.data[:500]!r}"
                )
            if r.status == 429 or 500 <= r.status < 600:
                if i == attempts - 1:
                    raise OpenAIConversationsAPIError(
                        f"OpenAI Conversations failed after {attempts} attempts: "
                        f"HTTP {r.status} {r.data[:200]!r}"
                    )
                log.warning(
                    "openai_conversations HTTP %s, retrying in %.1fs", r.status, backoff
                )
                time.sleep(backoff)
                backoff *= 2
                continue
            if r.status >= 400:
                raise OpenAIConversationsAPIError(
                    f"OpenAI Conversations HTTP {r.status}: {r.data[:500]!r}"
                )
            return json.loads(r.data)
        raise OpenAIConversationsAPIError("unreachable")


def _to_event(record: dict, parent: dict) -> AuditEvent:
    # Defensive normalization across plausible response shapes.
    rec_id = record.get("id") or record.get("message_id") or record.get("uuid")
    if not rec_id:
        # Derive a stable id from a content hash so dedupe still works
        # if the real spec doesn't include explicit ids.
        import hashlib

        canonical = json.dumps(record, sort_keys=True, separators=(",", ":"))
        rec_id = "synthetic_" + hashlib.sha256(canonical.encode()).hexdigest()[:32]

    ts_unix = (
        record.get("effective_at")
        or record.get("created_at")
        or parent.get("effective_at")
        or parent.get("created_at")
    )
    if isinstance(ts_unix, (int, float)):
        created_at = _unix_to_iso(int(ts_unix))
    elif isinstance(ts_unix, str):
        # If the spec emits ISO already, pass through.
        created_at = ts_unix
    else:
        # Last-resort fallback so we don't crash on missing timestamp.
        created_at = _unix_to_iso(int(datetime.now(timezone.utc).timestamp()))

    payload = {"conversation": parent, "message": record} if parent else record
    return AuditEvent(id=rec_id, created_at=created_at, vendor=VENDOR, raw=payload)


def _to_unix(dt: datetime) -> int:
    return int(dt.astimezone(timezone.utc).timestamp())


def _unix_to_iso(ts: int) -> str:
    return (
        datetime.fromtimestamp(int(ts), tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )
