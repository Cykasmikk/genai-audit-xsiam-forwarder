# OpenAI vendor setup

Detailed setup, key types, and spec conformance for the two
OpenAI-backed feeds (`openai` and `openai_conversations`).

## Enablement

| Surface | Enablement path |
|---|---|
| **Audit logging** (required for `openai`) | Org Owner: *Organization settings → Data controls → Data retention → Audit logging → Enable* |
| **Compliance Logs Platform** (required for `openai_conversations`) | OpenAI Enterprise / Edu plan with the platform onboarded — contact your OpenAI rep |

Without audit logging enabled, `/v1/organization/audit_logs` returns
no data even with a valid admin key.

## Admin key

OpenAI Audit Logs and Compliance APIs both use the same key type:

| Key | Prefix | Provisioned via | Capability |
|---|---|---|---|
| Admin API key | `sk-admin-...` | *Platform dashboard → Admin keys → Create new admin key* | Admin operations + Audit Logs + Compliance Platform (per OpenAI scope policy) |

Only Org Owners can create Admin keys. Standard project keys (`sk-...`)
and service account keys (`sk-svcacct-...`) cannot read audit logs or
the Compliance Platform.

## `openai` — Audit Logs API

**Endpoint:** `GET https://api.openai.com/v1/organization/audit_logs`

**Auth:** `Authorization: Bearer sk-admin-...`

**Query params:**

- `limit` — 1-100, default 20 (we use 100)
- `effective_at[gt|gte|lt|lte]` — Unix seconds, **bracket notation**
  (different from Anthropic's `created_at.gte` dotted notation)
- `after` / `before` — opaque cursor (just IDs, no `_id` suffix)
- Optional filters not used here: `project_ids[]`, `event_types[]`,
  `actor_ids[]`, `actor_emails[]`, `resource_ids[]`. We capture
  everything.

**Response:**

```json
{
  "data": [...],
  "first_id": "audit_log-xxx",
  "last_id": "audit_log-xxx",
  "has_more": bool
}
```

The adapter pages with `after={last_id}` until exhausted, then sorts
ascending by `(created_at, id)`. Note OpenAI emits `effective_at` as
**Unix seconds (int)**; the adapter converts to ISO 8601 for the common
`AuditEvent.created_at` shape, but **preserves `raw["effective_at"]`
as the original Unix int** so XSIAM operators can configure parsers
against the upstream OpenAI documentation directly.

**Audit log object schema:** `id`, `effective_at` (Unix seconds), `type`
(dotted-namespace, e.g. `api_key.created`), `actor` (nested
`actor.session.{ip_address, user.{id,email}}` or `actor.api_key.{id, type, ...}`),
`project` (`{id, name}` or null), plus type-specific payload (e.g.
`api_key.created` adds `{id, data: {scopes: [...]}}`).

51 documented event types — see
[docs/coverage.md](../coverage.md#openai--audit-logs-api-51-event-types).

## `openai_conversations` — ChatGPT conversation content

> **⚠️ Skeleton adapter.** The Compliance Logs Platform conversations
> endpoint is not publicly documented as of May 2026. The adapter
> ships with educated defaults aligned with the sibling Audit Logs
> API conventions, marked with `TODO(openai-conversations-spec)` at
> every uncertain point.

### Three paths to make this feed production-ready

#### Path A (best): use Palo Alto Networks' native integration

Per a Palo Alto community blog post in 2026, XSIAM has native support
for OpenAI ChatGPT Enterprise Compliance ingestion. Ask your XSIAM TAM
whether your tenant has the **OpenAI ChatGPT Enterprise Compliance**
data source available; if yes, prefer it over our skeleton — Palo
maintains spec drift. Don't deploy `openai_conversations` from this
repo in that case.

#### Path B: get the spec from your OpenAI rep

Override the placeholder via env var if the path differs:

```
OPENAI_CONVERSATIONS_PATH=/v1/<actual_path>
OPENAI_WORKSPACE_ID=<your workspace id>
```

If query parameter names or response shape differ, edit constants at
the top of `src/forwarder/vendors/openai_conversations.py`. Common
edits:

- `PARAM_LIMIT`, `PARAM_AFTER`, `PARAM_EFFECTIVE_AT_GTE/LTE` if the
  spec differs from the Audit Logs convention
- `RESP_DATA`, `RESP_HAS_MORE`, `RESP_LAST_ID` if response keys differ
- The branching in `fetch_window` between per-message and
  per-conversation response shapes — current code defensively handles
  both but you may want to commit to one once you have the spec

After editing, the smoke test in `tests/smoke.py` includes:

- `test_openai_conversations_handles_per_message_response`
- `test_openai_conversations_handles_per_conversation_response`
- `test_openai_conversations_synthesizes_id_when_missing`

These should still pass — they're shape-tolerant.

#### Path C: deploy and iterate against real responses

Deploy the skeleton against a low-traffic OpenAI org with audit
logging enabled. Read the 400 / 404 error bodies — OpenAI returns
specific messages naming unrecognized query params. Iterate.

### Defensive features in the skeleton

- **Both response shapes handled.** If the API returns flat per-message
  records (`data: [{id, effective_at, role, content}]`), we yield each
  as one event. If it returns per-conversation records with embedded
  messages (`data: [{id, messages: [...]}]`), we unpack and yield each
  message with the conversation metadata wrapped in.

- **Synthetic IDs.** If a record lacks an `id` field, the adapter
  computes a stable SHA-256 hash of the canonical-JSON-serialized
  record as `synthetic_<hash>` so dedupe still works.

- **404 message points at alternatives.** When the endpoint isn't
  found, the error message names both the env-var override and Palo
  Alto's native integration as alternatives.

## Self-audit

Per OpenAI: "All authenticated requests to this API are logged for
security and compliance purposes." The forwarder's own access shows up
in the Audit Logs feed. Same pattern as the Anthropic self-audit.

## Common pitfalls

- **Using a project key (sk-...) instead of admin key (sk-admin-...)**
  — 401. Issue a new Admin key as Org Owner.
- **Audit logging not enabled in org settings** — 403. Enable in the
  Data retention page.
- **OpenAI's February 2026 audit-log incident** — env-var dropped in a
  service split caused upstream data loss. We see XSIAM go quiet; we
  cannot recover the lost events. The 5-min overlap window helps
  during normal clock skew but not when OpenAI itself drops events.
  Recommend an XSIAM correlation rule: `login.succeeded` events should
  appear at the expected steady-state rate; alert on a sustained drop.
- **`openai_conversations` 404 on first run** — expected if you
  haven't done Path A or Path B above. The error message tells you
  what to do. Don't deploy this feed until you have the spec.
