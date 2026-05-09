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

## `openai_conversations` — Compliance Logs Platform

Conforms to the OpenAI cookbook spec at
<https://developers.openai.com/cookbook/examples/chatgpt/compliance_api/logs_platform>.

The Compliance Logs Platform is **architecturally different** from the
Audit Logs API:

- **Different host:** `api.chatgpt.com` (not `api.openai.com`)
- **Different key:** Compliance API key (provisioned via Compliance
  Platform onboarding) — distinct from the Admin key used for
  `/v1/organization/audit_logs`
- **Two-stage delivery:** list JSONL log files, then download each
  one's content. Each downloaded file is JSONL — one event per line
- **Workspace-scoped:** path is
  `/v1/compliance/{scope}/{principal_id}/logs` where `scope` is
  `workspaces` or `organizations` and `principal_id` is the workspace
  UUID or `org-…` id

### Configuration

The adapter requires:

| Env var | Default | Purpose |
|---|---|---|
| `OPENAI_PRINCIPAL_ID` | *required* — adapter fails closed if unset | Workspace UUID, or `org-…` id when scope=organizations |
| `OPENAI_PRINCIPAL_SCOPE` | `workspaces` | Either `workspaces` or `organizations` |
| `OPENAI_EVENT_TYPE` | unset (full-take) | Optional event-type filter (e.g. `conversations`, `auth`) |
| `OPENAI_COMPLIANCE_API_BASE` | `https://api.chatgpt.com` | Override for spec drift |
| `OPENAI_COMPLIANCE_LOGS_PATH_TEMPLATE` | `/v1/compliance/{scope}/{principal_id}/logs` | Override for spec drift |

The Compliance API key is the value of the `api_keys.openai_conversations`
Terraform variable. Its prefix is not publicly documented, so the
adapter doesn't enforce a specific prefix — it just requires
non-empty.

### Pagination model

Two-stage:

1. **List file pass:** repeatedly `GET …/logs?limit=&after=ISO8601&event_type=…`
   feeding `last_end_time` from the previous response back as `after`,
   until `has_more=false`.
2. **Download pass:** for each `file_id` in the listing, `GET …/logs/{id}`
   (urllib3 follows the signed-URL redirect by default). Parse the
   JSONL body, yield one `AuditEvent` per non-empty line.

Files whose `end_time` is past the upper bound of the requested
window are skipped client-side so the same window can be replayed
cleanly under the watermark+overlap dedupe model.

### Common pitfalls

- **`OPENAI_PRINCIPAL_ID` not set** — the adapter raises at
  construction time. Set it on the Lambda env (AWS) or Cloud Function
  env (GCP) via Terraform, or paste in the Workspace ID from your
  Compliance Platform onboarding screen.
- **Using the Admin key (`sk-admin-`) instead of the Compliance API
  key** — different key types, different scopes. The Compliance API
  key is issued during the Compliance Platform onboarding flow.
- **Pointing at `api.openai.com`** — wrong host. Use `api.chatgpt.com`.

### Alternative: Palo Alto's native integration

Per a Palo Alto community post in 2026, XSIAM has native support for
OpenAI ChatGPT Enterprise Compliance. If your tenant has the **OpenAI
ChatGPT Enterprise Compliance** data source available in the XSIAM UI,
prefer it over this adapter — Palo maintains spec drift. Both options
are documented; pick whichever fits your XSIAM data-source strategy.

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
