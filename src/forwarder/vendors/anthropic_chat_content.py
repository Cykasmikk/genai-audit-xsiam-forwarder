"""Anthropic Compliance API — chat content adapter.

This is a SECOND Anthropic adapter, distinct from `anthropic_compliance.py`:

  - `anthropic_compliance.py` pulls the **Activity Feed** (admin/auth/system
    events) — metadata only.
  - This adapter pulls **chat messages** including their full content
    (user prompts, assistant responses, tool calls, file references) from
    the Compliance API content endpoints.

Rev J 2026-04-20 endpoints used:
  - GET /v1/compliance/apps/chats               — list chats in window
  - GET /v1/compliance/apps/chats/{id}/messages — full content per chat

Auth requirement (different from the Activity Feed adapter!):
  - Compliance Access Key (`sk-ant-api01-...`) with the
    `read:compliance_user_data` scope. **Admin keys (sk-ant-admin01-) do
    NOT work for content endpoints** — they only authorize the Activity
    Feed.
  - Compliance Access Keys are issued via Claude.ai → Org settings →
    Data and Privacy → Compliance access keys (Claude.ai-only feature).

Vendor key
----------
This adapter publishes under the vendor key **`anthropic_chats`** (distinct
from `anthropic` which is the Activity Feed). State, S3 prefix, Pub/Sub
topic, and XSIAM data source are all separate, so the SOC operator gets
two clean datasets per Anthropic feed and isn't forced to disambiguate
admin events from chat content in XQL.

Volume warning
--------------
Chat content volume can be 10–1000× audit-feed volume. SOC retention,
PII review, and XSIAM ingestion cost should be reviewed before deploying
this adapter. The `INITIAL_LOOKBACK_MINUTES` default of 60 still applies,
but consider a much smaller value for first deploy in a busy org.

Optional file-content fetch
---------------------------
If `ANTHROPIC_FETCH_FILE_CONTENT=1`, file references in messages are
expanded by calling
`GET /v1/compliance/apps/chats/files/{file_id}/content` and inlining
the response body as base64 on the file reference. Off by default
because file content can balloon payload size (and XSIAM has per-event
size limits). When off, the metadata (file_id, filename, mime_type) is
forwarded so the SOC can fetch on-demand for investigations.
"""

from __future__ import annotations

import base64
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

VENDOR = "anthropic_chats"

ANTHROPIC_API_BASE = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"

CHATS_LIST_PATH = os.environ.get(
    "ANTHROPIC_CHATS_LIST_PATH", "/v1/compliance/apps/chats"
)
# {claude_chat_id} placeholder
CHAT_MESSAGES_PATH_TEMPLATE = os.environ.get(
    "ANTHROPIC_CHAT_MESSAGES_PATH_TEMPLATE",
    "/v1/compliance/apps/chats/{chat_id}/messages",
)
FILE_CONTENT_PATH_TEMPLATE = os.environ.get(
    "ANTHROPIC_FILE_CONTENT_PATH_TEMPLATE",
    "/v1/compliance/apps/chats/files/{file_id}/content",
)

PARAM_LIMIT = "limit"
PARAM_AFTER_ID = "after_id"
# Use updated_at filter so we pick up chats whose messages changed in the
# window, not just chats that were created in it.
PARAM_UPDATED_AT_GTE = "updated_at.gte"
PARAM_UPDATED_AT_LTE = "updated_at.lte"
RESP_DATA = "data"
RESP_HAS_MORE = "has_more"
RESP_LAST_ID = "last_id"

CHATS_LIST_PAGE_LIMIT = 1000  # Rev J default 100, max 1000

# Compliance Access Keys only.
_VALID_KEY_PREFIX = "sk-ant-api01-"


class AnthropicChatContentAPIError(RuntimeError):
    pass


class AnthropicChatContentClient:
    vendor = VENDOR

    def __init__(
        self,
        api_key: str,
        api_base: str = ANTHROPIC_API_BASE,
        chats_list_path: str = CHATS_LIST_PATH,
        chat_messages_path_template: str = CHAT_MESSAGES_PATH_TEMPLATE,
        file_content_path_template: str = FILE_CONTENT_PATH_TEMPLATE,
        fetch_file_content: bool | None = None,
        http: urllib3.PoolManager | None = None,
    ):
        if not api_key.startswith(_VALID_KEY_PREFIX):
            raise ValueError(
                "Anthropic chat content endpoints require a Compliance "
                "Access Key (sk-ant-api01-...) with read:compliance_user_data "
                "scope. Admin keys (sk-ant-admin01-) only authorize the "
                "Activity Feed and will return 401 for content. Issue a "
                "Compliance Access Key via Claude.ai → Org settings → Data "
                "and Privacy → Compliance access keys (Claude.ai-only)."
            )
        self._key = api_key
        self._base = api_base.rstrip("/")
        self._chats_list_path = chats_list_path
        self._chat_messages_path_template = chat_messages_path_template
        self._file_content_path_template = file_content_path_template
        if fetch_file_content is None:
            fetch_file_content = (
                os.environ.get("ANTHROPIC_FETCH_FILE_CONTENT", "0") == "1"
            )
        self._fetch_file_content = fetch_file_content
        self._http = http or urllib3.PoolManager(retries=False, timeout=60.0)

    def _headers(self) -> dict:
        return {
            "x-api-key": self._key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
            "user-agent": "genai-audit-xsiam-forwarder/2.0",
        }

    def fetch_window(
        self,
        starting_at: datetime,
        ending_at: datetime,
    ) -> Iterator[AuditEvent]:
        """Yield one AuditEvent per chat message whose chat was updated in
        the window. Each event's id is the message id; created_at is the
        message timestamp (NOT the chat's). This makes dedupe message-level.
        """
        for chat in self._iter_chats(starting_at, ending_at):
            chat_id = chat["id"]
            try:
                full = self._get_chat_messages(chat_id)
            except AnthropicChatContentAPIError as e:
                # Don't let a single bad chat abort the whole run.
                log.warning("anthropic_chats: skipping chat %s due to %s", chat_id, e)
                continue

            chat_meta = {k: v for k, v in full.items() if k != "chat_messages"}

            for msg in full.get("chat_messages") or []:
                if self._fetch_file_content:
                    msg = self._inline_file_content(msg)
                payload = {
                    "chat": chat_meta,
                    "message": msg,
                }
                yield AuditEvent(
                    id=msg["id"],
                    created_at=msg["created_at"],
                    vendor=VENDOR,
                    raw=payload,
                )

    def _iter_chats(self, starting_at: datetime, ending_at: datetime) -> Iterator[dict]:
        base_params = {
            PARAM_LIMIT: CHATS_LIST_PAGE_LIMIT,
            PARAM_UPDATED_AT_GTE: _iso_z(starting_at),
            PARAM_UPDATED_AT_LTE: _iso_z(ending_at),
        }
        after_id: str | None = None
        page_count = 0
        while True:
            params = dict(base_params)
            if after_id:
                params[PARAM_AFTER_ID] = after_id
            url = f"{self._base}{self._chats_list_path}?{urlencode(params)}"
            payload = self._request_with_retry("GET", url)
            for chat in payload.get(RESP_DATA, []):
                yield chat
            page_count += 1
            if not payload.get(RESP_HAS_MORE):
                break
            next_cursor = payload.get(RESP_LAST_ID)
            if not next_cursor or next_cursor == after_id:
                log.warning(
                    "anthropic_chats: has_more=true but last_id missing/unchanged"
                )
                break
            after_id = next_cursor
        log.info(
            "anthropic_chats: listed chats updated [%s, %s] pages=%d",
            base_params[PARAM_UPDATED_AT_GTE],
            base_params[PARAM_UPDATED_AT_LTE],
            page_count,
        )

    def _get_chat_messages(self, chat_id: str) -> dict:
        path = self._chat_messages_path_template.format(chat_id=chat_id)
        url = f"{self._base}{path}"
        return self._request_with_retry("GET", url)

    def _inline_file_content(self, msg: dict) -> dict:
        files = msg.get("files") or []
        if not files:
            return msg
        new_files = []
        for f in files:
            file_id = f.get("id")
            if not file_id:
                new_files.append(f)
                continue
            try:
                content = self._fetch_file_bytes(file_id)
                new_files.append(
                    {
                        **f,
                        "content_base64": base64.b64encode(content).decode("ascii"),
                    }
                )
            except AnthropicChatContentAPIError as e:
                log.warning(
                    "anthropic_chats: failed to fetch file %s content: %s", file_id, e
                )
                new_files.append({**f, "content_fetch_error": str(e)})
        return {**msg, "files": new_files}

    def _fetch_file_bytes(self, file_id: str) -> bytes:
        path = self._file_content_path_template.format(file_id=file_id)
        url = f"{self._base}{path}"
        # Files endpoint returns binary, not JSON — use a raw request.
        backoff = 1.0
        for i in range(4):
            r = self._http.request("GET", url, headers=self._headers())
            if r.status == 200:
                return r.data
            if r.status == 429 or 500 <= r.status < 600:
                if i == 3:
                    raise AnthropicChatContentAPIError(
                        f"file fetch failed after retries: HTTP {r.status}"
                    )
                time.sleep(backoff)
                backoff *= 2
                continue
            raise AnthropicChatContentAPIError(
                f"file fetch HTTP {r.status}: {r.data[:200]!r}"
            )
        raise AnthropicChatContentAPIError("unreachable")

    def _request_with_retry(self, method: str, url: str, attempts: int = 4) -> dict:
        backoff = 1.0
        for i in range(attempts):
            r = self._http.request(method, url, headers=self._headers())
            if r.status == 404:
                raise AnthropicChatContentAPIError(
                    f"Anthropic chat content path not found: {url}. "
                    "Per Rev J 2026-04-20 the paths are "
                    "/v1/compliance/apps/chats and "
                    "/v1/compliance/apps/chats/{id}/messages. Override via "
                    "ANTHROPIC_CHATS_LIST_PATH / "
                    "ANTHROPIC_CHAT_MESSAGES_PATH_TEMPLATE if a newer "
                    f"revision moved them. Response: {r.data[:200]!r}"
                )
            if r.status in (401, 403):
                raise AnthropicChatContentAPIError(
                    f"Anthropic chat content auth rejected (HTTP {r.status}). "
                    "Verify: (a) Compliance API is enabled, (b) the key is a "
                    "Compliance Access Key (sk-ant-api01-) NOT an Admin key, "
                    "(c) the key has read:compliance_user_data scope. "
                    f"Response: {r.data[:200]!r}"
                )
            if r.status == 400:
                raise AnthropicChatContentAPIError(
                    f"Anthropic chat content rejected request (HTTP 400): {r.data[:500]!r}"
                )
            if r.status == 429 or 500 <= r.status < 600:
                if i == attempts - 1:
                    raise AnthropicChatContentAPIError(
                        f"Anthropic chat content failed after {attempts} attempts: "
                        f"HTTP {r.status} {r.data[:200]!r}"
                    )
                log.warning(
                    "anthropic_chats HTTP %s, retrying in %.1fs", r.status, backoff
                )
                time.sleep(backoff)
                backoff *= 2
                continue
            if r.status >= 400:
                raise AnthropicChatContentAPIError(
                    f"Anthropic chat content HTTP {r.status}: {r.data[:500]!r}"
                )
            return json.loads(r.data)
        raise AnthropicChatContentAPIError("unreachable")


def _iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
