# Anthropic vendor setup

Detailed setup, key types, and spec conformance for the two
Anthropic-backed feeds (`anthropic` and `anthropic_chats`) plus the
Cowork OTel pipeline that pairs with them.

## Enablement

The Compliance API is gated behind the Anthropic Enterprise plan and
must be explicitly turned on:

| Surface | Enablement path |
|---|---|
| **Claude.ai** | Primary Owner: *Org settings → Data and Privacy → Compliance API → Enable* |
| **Console / Claude API** | Org admin requests via Anthropic account team |

When enabled, two key types become available:

| Key | Prefix | Provisioned via | Scope |
|---|---|---|---|
| Admin key | `sk-ant-admin01-...` | Console → Settings → Admin keys | `read:compliance_activities` only (Activity Feed) |
| Compliance Access Key | `sk-ant-api01-...` | Claude.ai → Org settings → Data and Privacy → Compliance access keys | One or more of: `read:compliance_activities`, `read:compliance_user_data`, `delete:compliance_user_data`, `read:compliance_org_data` |

**Key choice for this repo:**

| Feed | Key |
|---|---|
| `anthropic` | Either Admin or Compliance Access (Admin is simpler) |
| `anthropic_chats` | **Compliance Access only** — Admin keys 401 on content endpoints. Needs `read:compliance_user_data` scope. |

## Spec conformance

The `anthropic_compliance.py` and `anthropic_chat_content.py` adapters
conform to the **Compliance API: Activity Feed, Chats, Files,
Organizations, Users, and Projects — Rev J 2026-04-20** PDF distributed
by Anthropic to Enterprise customers with the Compliance API enabled.

If you receive a newer revision (Rev K, etc.) and find that the
endpoint paths or pagination conventions changed, override via env vars:

```
ANTHROPIC_COMPLIANCE_API_PATH=/v1/<new path>            # /v1/compliance/activities by default
ANTHROPIC_CHATS_LIST_PATH=/v1/<new path>                # /v1/compliance/apps/chats by default
ANTHROPIC_CHAT_MESSAGES_PATH_TEMPLATE=/v1/<new>/{chat_id}/messages
ANTHROPIC_FILE_CONTENT_PATH_TEMPLATE=/v1/<new>/files/{file_id}/content
```

If field names changed inside the response shape, edit the constants
at the top of the adapter files.

## `anthropic` — Activity Feed

**Endpoint:** `GET https://api.anthropic.com/v1/compliance/activities`

**Auth:** `x-api-key` with `sk-ant-admin01-...` or `sk-ant-api01-...`
(both work for this endpoint).

**Query params:**

- `limit` — page size (default 100, max 5000; we use 1000)
- `created_at.gte` / `.lte` — RFC 3339 inclusive bounds
- `after_id` / `before_id` — opaque cursor (activity ID)
- Optional filters not used by this forwarder: `organization_ids[]`,
  `actor_ids[]`, `activity_types[]`. We do not filter — we capture
  everything.

**Response:**

```json
{
  "data": [...],          // newest-first within page
  "has_more": bool,
  "first_id": "activity_xxx",
  "last_id": "activity_xxx"
}
```

The adapter pages forward in time using `after_id={last_id}` until
`has_more=false`, then sorts ascending by `(created_at, id)` and
yields oldest-first so the watermark can advance monotonically.

**Activity object schema:** `id`, `created_at` (RFC 3339), `type`
(snake_case event type), `actor` (discriminated union — see
`docs/coverage.md`), plus type-specific extra fields. ~200 documented
event types. See [docs/coverage.md](../coverage.md#anthropic--compliance-api-activity-feed-rev-j) for the full
inventory.

## `anthropic_chats` — chat content

**Endpoints:**

- `GET /v1/compliance/apps/chats` — list chats updated in window
- `GET /v1/compliance/apps/chats/{claude_chat_id}/messages` — full
  message history per chat
- `GET /v1/compliance/apps/chats/files/{claude_file_id}/content` —
  binary file download (only used if `ANTHROPIC_FETCH_FILE_CONTENT=1`)

**Auth:** Compliance Access Key (`sk-ant-api01-...`) with
`read:compliance_user_data` scope. Admin keys are explicitly rejected
by the adapter with an actionable error.

**List query params:** `updated_at.gte` / `.lte` (we use updated_at,
not created_at, so we pick up chats whose messages changed in the
window — not just new chats), `after_id`, `limit`.

**Per-chat response:** chat metadata + full `chat_messages` array.

**Forwarder behavior:**

- One `AuditEvent` per chat **message** (not per chat), so dedupe
  granularity is message-level and the watermark advances on each
  message's `created_at`.
- The wrapped payload is `{chat: <metadata>, message: <body>}` so
  XSIAM operators see chat context with every message event.
- File content is **off** by default. Set
  `ANTHROPIC_FETCH_FILE_CONTENT=1` to inline file bytes as base64 on
  each `files[].content_base64` field. Caveat: file payloads can blow
  past XSIAM's per-event size cap; default-off is the safe choice and
  the `file_id` is preserved either way for on-demand retrieval.
- A failed fetch on one chat (5xx after retries) skips that chat
  rather than aborting the whole run — see
  `_get_chat_messages` exception handling in the adapter.

## Cowork OpenTelemetry — sibling pipeline

For inference-level visibility (prompts, tool calls, file access, model
+ token + cost per request), deploy the standalone OTel collector:

- See [cowork-otel/README.md](../../cowork-otel/README.md) for the
  collector deployment.
- See `docs/coverage.md` § Cowork OTel for the event content.

**Cowork agent-side configuration (Anthropic admin portal):**

*Organization settings → Cowork → OpenTelemetry endpoint* — set:

- OTLP endpoint: `https://<your-collector-hostname>/v1/logs`
- OTLP protocol: HTTP/JSON or HTTP/protobuf
- Headers: `Authorization: Bearer <token from Terraform>`

**Claude Code workstation configuration** via IT-managed settings:

```bash
CLAUDE_CODE_ENABLE_TELEMETRY=1
OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
OTEL_EXPORTER_OTLP_ENDPOINT=https://<your-collector-hostname>
OTEL_EXPORTER_OTLP_HEADERS=Authorization=Bearer\ <token>
OTEL_LOGS_EXPORTER=otlp
OTEL_METRICS_EXPORTER=otlp
OTEL_LOG_USER_PROMPTS=1     # opt-in: capture prompt text
OTEL_LOG_TOOL_DETAILS=1     # opt-in: capture tool args
```

Without `OTEL_LOG_USER_PROMPTS=1` and `OTEL_LOG_TOOL_DETAILS=1`,
Claude Code redacts prompts and tool args by default. For full-take
SOC capture you want these on; ensure your IT and legal teams have
signed off on capturing these in the SOC dataset.

## Self-audit

Every Compliance API request emits a `compliance_api_accessed`
Activity event (page 11 of Rev J PDF). The forwarder appears in the
feed it forwards. Useful XQL:

```xql
dataset = <anthropic_dataset>
| filter type = "compliance_api_accessed"
| filter actor.api_key_id = "<forwarder's api key id>"
| comp count() by bin(_time, 5m)
```

A drop-to-zero indicates the forwarder is down. An anomalous source IP
indicates key compromise. See [docs/operations.md](../operations.md#alarms)
for alarm recommendations.

## Common pitfalls

- **Using an Admin key for `anthropic_chats`** — content endpoints
  401 with Admin keys. Use a Compliance Access Key with
  `read:compliance_user_data` scope.
- **Compliance API not enabled but key issued** — keys provision
  before enablement is processed. The first run returns 403; wait for
  enablement to propagate then retry.
- **Forgetting to issue a Compliance Access Key for sub-organizations**
  — Compliance Access Keys at the parent-org level cover all linked
  organizations (per Rev J). If your org structure has multiple linked
  Claude Enterprise orgs, set `organization_ids[]` to span them or
  filter XSIAM-side.
- **`ANTHROPIC_FETCH_FILE_CONTENT=1` flooding XSIAM** with binary blobs
  — leave it off unless your retention/cost analysis says otherwise.
  Pull file content on-demand for investigations via the Compliance
  API endpoint.
