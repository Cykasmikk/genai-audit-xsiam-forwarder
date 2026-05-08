# genai-audit-xsiam-forwarder

Forwards GenAI platform audit logs **and conversation content** into
**Cortex XSIAM** using the cloud-native ingestion patterns documented by
Palo Alto Networks. Vendor-adapter architecture — drop in a new adapter
to add another provider.

**Full-take coverage** is the design goal: admin/auth metadata, resource
activity, AND prompt/response content for SOC investigations. Five feeds
across two vendors plus an OpenTelemetry collector for Claude Code /
Cowork inference.

| Feed | Source | What it captures | Spec conformance |
|---|---|---|---|
| `anthropic` | Compliance API — Activity Feed (`/v1/compliance/activities`) | Admin/auth/system events, resource activity (~200 types) | Rev J 2026-04-20 |
| `anthropic_chats` | Compliance API — chat content (`/v1/compliance/apps/chats/{id}/messages`) | **Full chat transcripts** including prompts, responses, file refs, artifacts | Rev J 2026-04-20 |
| `openai` | Audit Logs API (`/v1/organization/audit_logs`) | Admin/auth events (51 types) | platform.openai.com /docs/api-reference/audit-logs |
| `openai_conversations` | Compliance Logs Platform — conversation logs | **Full ChatGPT conversation transcripts** (Enterprise/Edu) | ⚠️ partial spec — see [Coverage gaps](#coverage-gaps) |
| Cowork OTel | OTel Collector receiving OTLP from Claude Cowork + Claude Code agents | **Prompts, tool calls, file access, model + token + cost per request** | Standard OTel logs/metrics |

**Ingest paths (per cloud):**

- **AWS:** Lambda → S3 (gzipped JSON-lines under `<vendor>/`) → S3 ObjectCreated → SQS → XSIAM pulls via cross-account IAM role with external ID. One SQS queue per vendor; one shared bucket and IAM role.
- **GCP:** Cloud Function → Pub/Sub topic (one per vendor) → XSIAM pulls via per-vendor pull subscription with a shared service account.
- **Fallback:** direct POST to XSIAM HTTP Collector (documented; not default).

There is no native Anthropic or OpenAI integration in XSIAM, and no vendor-published XSIAM connector. This repo is the custom forwarder.

## Coverage matrix

| What you want to forward | Feed | Notes |
|---|---|---|
| Anthropic admin/auth/resource events | `anthropic` | Admin key (`sk-ant-admin01-`) sufficient |
| Claude.ai chat prompts + responses | `anthropic_chats` | **Compliance Access Key (`sk-ant-api01-`) required**, Admin keys won't work |
| Claude.ai file uploads (binary content) | `anthropic_chats` with `ANTHROPIC_FETCH_FILE_CONTENT=1` | Off by default — see [Volume warning](#volume-warning) |
| Claude API (programmatic) prompts/responses | **Not available** server-side | Anthropic does not retain API call bodies — log application-side or via a proxy |
| Claude Code prompts + tool calls | Cowork OTel collector | Set `OTEL_LOG_USER_PROMPTS=1` + `OTEL_LOG_TOOL_DETAILS=1` in managed settings |
| Cowork prompts + tool calls + approvals | Cowork OTel collector | Configured centrally in Anthropic admin portal |
| OpenAI admin/auth/project events | `openai` | Admin key (`sk-admin-`) |
| ChatGPT Enterprise/Edu conversation prompts + responses | `openai_conversations` | **⚠️ Partial spec — endpoint not publicly documented**, see [Coverage gaps](#coverage-gaps) |
| OpenAI API (programmatic) prompts/responses | **Not available** server-side | Use OpenAI's `store=true` Responses API or proxy-side logging |
| AWS Bedrock Claude usage | Out of scope | CloudTrail Bedrock data events (separate XSIAM data source) |
| Azure OpenAI usage | Out of scope | Azure Monitor / Activity Logs (separate XSIAM data source) |

### Coverage gaps

1. **OpenAI Conversations endpoint path is not publicly documented.**
   The `openai_conversations` adapter ships with educated defaults
   aligned with the sibling Audit Logs API (Authorization Bearer,
   `effective_at[gte]/[lte]` Unix-seconds bracket filter, `after`
   pagination), marked with `TODO(openai-conversations-spec)`. Either
   get the spec from your OpenAI Enterprise rep and override
   `OPENAI_CONVERSATIONS_PATH` if needed, OR use Palo Alto Networks'
   native XSIAM "OpenAI ChatGPT Enterprise Compliance" integration
   (announced 2026) — verify availability with your XSIAM TAM.

2. **Programmatic API call bodies** (Claude API, OpenAI API) are not
   retained by either vendor. To audit them, you need application-side
   logging or a logging proxy in front of the API.

### Volume warning

The two content feeds (`anthropic_chats`, `openai_conversations`) and
the Cowork OTel collector all carry full prompt/response text. Volume
is 10–1000× the audit feeds. Enable them only after you've reviewed:

- **XSIAM ingestion costs** — pricing is volume-based.
- **PII / data-classification policy** — prompts can contain regulated
  data. Consider XSIAM-side redaction processors or scope feeds to
  specific datasets with stricter access control.
- **First-run lookback** — the default 60-min window is sane for audit
  feeds but `INITIAL_LOOKBACK_MINUTES=10` is safer for first deploy of
  content feeds in a busy org.

## Architecture

### AWS — native pattern (default)

```
   ┌─────────────┐  rate(5min)  ┌──────────────┐   PutObject
   │ EventBridge │ ──────────▶  │   Lambda     │ ─────────────┐
   │ (per vendor)│              │ (per vendor) │              ▼
   └─────────────┘              └──────┬───────┘     ┌─────────────────┐
                                       │             │ Shared bucket   │
              ┌────────────────────────┘             │  anthropic/...  │
              ▼                                      │  openai/...     │
      ┌──────────────────┐                           └────────┬────────┘
      │ Anthropic /      │                                    │ ObjectCreated
      │ OpenAI audit API │                                    │ (prefix-filtered)
      └──────────────────┘                                    ▼
                                                    ┌──────────────────┐
                                                    │ SQS per vendor   │
                                                    │  (DLQ each)      │
                                                    └─────────┬────────┘
                                                              │ XSIAM polls
                                                              ▼
                                                    ┌──────────────────┐
                                                    │   Cortex XSIAM   │
                                                    │   (one DS per    │
                                                    │   vendor; shared │
                                                    │   assumed role)  │
                                                    └──────────────────┘
```

### GCP — native pattern (default)

```
   ┌─────────────┐  cron */5    ┌────────────────┐    publish    ┌────────────────┐
   │  Scheduler  │ ─────────▶   │ Cloud Function │ ──────────▶   │ audit topic    │
   │ (per vendor)│              │  (per vendor)  │               │  (per vendor)  │
   └─────────────┘              └────────┬───────┘               └────────┬───────┘
                                         │                                │
        ┌────────────────────────────────┘                                │
        ▼                                                                 ▼
  ┌──────────────────┐                                            ┌────────────────┐
  │ Anthropic /      │                                            │ pull sub per   │
  │ OpenAI audit API │                                            │   vendor       │
  └──────────────────┘                                            └────────┬───────┘
                                                                           │ XSIAM pulls
                                                                           ▼
                                                                ┌──────────────────┐
                                                                │  Cortex XSIAM    │
                                                                │  (one DS per     │
                                                                │  vendor; shared  │
                                                                │  SA credential)  │
                                                                └──────────────────┘
```

### Parallel execution

The repo is designed for **both vendors running concurrently** with no
shared mutable state at runtime:

- **Per-vendor Lambda / Cloud Function.** Terraform `for_each` over the
  vendors map produces one function, one schedule, one queue/topic, and
  one secret per vendor. They have separate IAM principals and separate
  log groups. They invoke at the same wall-clock minute and run truly in
  parallel.
- **State is vendor-namespaced.** DynamoDB PK is `{vendor}_audit_state`;
  Firestore doc id is `{vendor}_state`. A read or write for one vendor
  cannot read or clobber another vendor's row.
- **Egress is vendor-partitioned.** S3 keys carry a `{vendor}/` prefix and
  per-vendor SQS queues filter on it. Pub/Sub uses one topic and one pull
  subscription per vendor. XSIAM operators wire up one data source per
  vendor; events never cross-pollinate.
- **Same-vendor overlap is serialized.** Each Lambda has
  `reserved_concurrent_executions = 1` and each Cloud Function has
  `max_instance_count = 1` — if one tick exceeds the schedule interval,
  the next invocation is queued (Lambda) or the Pub/Sub delivery is
  retried (Cloud Function), so a slow Anthropic poll never races the next
  Anthropic poll on the state row. Cross-vendor concurrency is
  unaffected: the OpenAI invocation runs concurrently with the slow
  Anthropic one.

The smoke suite includes `test_parallel_execution_no_contention` (both
vendors fired from threads against a shared state simulator) and
`test_parallel_repeated_runs_dedupe_correctly` (two same-vendor invocations
hammered in parallel, asserting no payload mutation or fabrication).

### Idempotency model

Both vendors emit stable per-event IDs. Each tick, per vendor:

1. Loads vendor's prior watermark + recent IDs from DynamoDB / Firestore.
2. Queries `[watermark - 5min, now]` (overlap window absorbs clock skew).
3. Drops events whose `id` is already in `recent_ids`.
4. Forwards survivors to the egress sink.
5. Persists the advanced watermark + refreshed ID set **only after** the egress sink ACKs.

State documents are namespaced by vendor (`{vendor}_audit_state` PK / `{vendor}_state` doc), so vendors share the table/collection without cross-contamination.

## Vendor adapters

Each adapter in `src/forwarder/vendors/` normalizes its native payload to a common `AuditEvent` (`id`, ISO `created_at`, `vendor`, `raw`). The vendor-native payload is preserved in `raw` and forwarded verbatim — XSIAM operators configure parsers against the original schema without translation gotchas.

| Element | Anthropic | OpenAI |
|---|---|---|
| Endpoint | `/v1/compliance/activities` | `/v1/organization/audit_logs` |
| Auth header | `x-api-key: sk-ant-admin01-` or `sk-ant-api01-` | `Authorization: Bearer sk-admin-` |
| Time field | `created_at` (RFC 3339) | `effective_at` (Unix sec) — adapter converts to ISO |
| Time filter | `created_at.gte=...` (dotted) | `effective_at[gte]=...` (bracketed) |
| Pagination | `after_id` / `before_id` | `after` / `before` |
| Page limit | default 100, max 5000 | default 20, max 100 |
| Event id field | `id` (`activity_xxx`) | `id` (`audit_log-xxx`) |
| Event type | `claude_chat_created` (snake) | `api_key.created` (dotted) |
| Actor structure | flat with `actor.type` discriminator | nested: `actor.session` or `actor.api_key` |

## Prerequisites

### Anthropic

1. **Claude Enterprise plan** (Compliance API is GA on Enterprise, excluding Public Sector).
2. **Compliance API enabled** — Claude.ai Primary Owner via *Org settings → Data and Privacy → Compliance API*, OR Console/API admin requests via Anthropic account team.
3. **Admin key** (`sk-ant-admin01-...`) via *Console → Settings → Admin keys* (Activity Feed access only — fine for SOC), OR **Compliance Access Key** (`sk-ant-api01-...`) via *Claude.ai → Org settings → Data and Privacy → Compliance access keys* (broader scopes, Claude.ai-only feature).

### OpenAI

1. **Organization Owner** (only Owners can provision admin keys).
2. **Audit logging enabled** — *Organization settings → Data controls → Data retention → Audit logging → Enable*. Without this the endpoint returns no data.
3. **Admin API key** (`sk-admin-...`) via *Platform dashboard → Admin keys → Create new admin key*.

### Both

4. **XSIAM data source onboarding info** — depends on path:
   - **AWS:** the AWS account ID of your XSIAM tenant (shown in *Add data source → Amazon S3 generic logs*).
   - **GCP:** none in advance. After `apply` you generate a SA key and paste it (with each subscription name) into one *GCP Pub/Sub* data source per vendor.
   - **HTTP fallback:** an HTTP Collector configured as a Custom App.
5. Terraform ≥ 1.6.

## Repository layout

```
src/
  main.py                       GCP Cloud Function entrypoint
  requirements.txt              GCP Cloud Build installs these
  forwarder/
    core.py                     vendor-agnostic fetch → forward → checkpoint
    state.py                    ForwarderState + StateStore protocol
    state_aws.py                DynamoDB state backend (per-vendor PK)
    state_gcp.py                Firestore state backend (per-vendor doc)
    aws_handler.py              Lambda entrypoint (VENDOR env var dispatch)
    gcp_handler.py              Cloud Function handler
    vendors/
      __init__.py               AuditClient protocol + AuditEvent
      anthropic_compliance.py   Anthropic Compliance API (Rev J)
      openai_audit.py           OpenAI Audit Logs API
    egress/
      __init__.py               Egress protocol
      s3.py                     AWS native: gzipped JSON-lines (vendor-prefixed)
      pubsub.py                 GCP native: Pub/Sub (vendor attribute + extras)
      http.py                   Fallback: XSIAM HTTP Collector envelope
terraform/aws/                  Per-vendor Lambda/EventBridge/SQS/Secret +
                                shared bucket/state-table/IAM-role
terraform/gcp/                  Per-vendor Function/Scheduler/Pub-Sub topic+sub/Secret +
                                shared Firestore/SA
cowork-otel/                    Standalone OTel Collector deployment for
                                Cowork + Claude Code (separate from polling
                                forwarder). See cowork-otel/README.md.
tests/smoke.py                  47 deterministic tests (no AWS/GCP creds needed)
.github/workflows/ci.yml        Python smoke + Terraform validate per PR
```

## Deploy — AWS

```hcl
# terraform/aws/example.tfvars (gitignored)
vendors = {
  anthropic            = { schedule_minutes = 5 }
  anthropic_chats      = { schedule_minutes = 5, initial_lookback_minutes = 10 }
  openai               = { schedule_minutes = 5 }
  openai_conversations = { schedule_minutes = 5, initial_lookback_minutes = 10 }
}
api_keys = {
  anthropic            = "sk-ant-admin01-..."  # Admin key — Activity Feed only
  anthropic_chats      = "sk-ant-api01-..."    # Compliance Access Key — content
  openai               = "sk-admin-..."        # Admin key — Audit Logs
  openai_conversations = "sk-admin-..."        # Admin key — Conversations
}
xsiam_aws_account_id = "123456789012"
```

To deploy a subset of feeds, omit them from both maps. Each feed gets its
own Lambda, SQS queue, and S3 prefix; XSIAM operators configure one data
source per feed.

```bash
cd terraform/aws
terraform init
terraform apply -var-file=example.tfvars
```

After `apply`, configure **one XSIAM data source per vendor** at *Add data source → Amazon S3 generic logs*:

| XSIAM field    | Terraform output                                |
|----------------|-------------------------------------------------|
| Role ARN       | `xsiam_role_arn` (shared across vendors)        |
| External ID    | `xsiam_external_id` (sensitive, shared)         |
| SQS queue URL  | `xsiam_sqs_urls[<vendor>]`                      |
| Bucket         | `audit_bucket` (shared)                         |

```bash
terraform output xsiam_sqs_urls
terraform output -raw xsiam_external_id
```

## Deploy — GCP

```hcl
# terraform/gcp/example.tfvars (gitignored)
project_id = "my-soc-project"
region     = "us-central1"
vendors = {
  anthropic            = { schedule_minutes = 5 }
  anthropic_chats      = { schedule_minutes = 5, initial_lookback_minutes = 10 }
  openai               = { schedule_minutes = 5 }
  openai_conversations = { schedule_minutes = 5, initial_lookback_minutes = 10 }
}
api_keys = {
  anthropic            = "sk-ant-admin01-..."
  anthropic_chats      = "sk-ant-api01-..."
  openai               = "sk-admin-..."
  openai_conversations = "sk-admin-..."
}
```

```bash
cd terraform/gcp
terraform init
terraform apply -var-file=example.tfvars
```

After `apply`:

1. Generate a JSON key for the **shared** XSIAM service account (used for both vendors):
   ```bash
   gcloud iam service-accounts keys create xsiam-credentials.json \
     --iam-account=$(terraform output -raw xsiam_service_account_email)
   ```

2. Configure **one XSIAM data source per vendor** at *Add data source → GCP Pub/Sub*:

   | XSIAM field        | Source                                                |
   |--------------------|-------------------------------------------------------|
   | Subscription name  | `terraform output xsiam_audit_subscriptions` (per vendor) |
   | Service account    | contents of `xsiam-credentials.json`                  |

3. Delete the local key file once XSIAM has it.

## Verifying ingestion

After the first scheduled run:

```xql
// All vendors at a glance — partition by vendor metadata
dataset = <your_audit_dataset>
| sort desc _time
| limit 100
```

```xql
// Anthropic-specific: every Compliance API request is itself logged
dataset = <your_anthropic_audit_dataset>
| filter type = "compliance_api_accessed"
| fields _time, actor.type, actor.api_key_id, url, status_code
```

```xql
// OpenAI: failed login spike detection
dataset = <your_openai_audit_dataset>
| filter type = "login.failed"
| comp count() by bin(_time, 5m), actor.session.user.email
```

```xql
// Cross-vendor: every API key created in the last 24h, any provider
dataset in (<anthropic>, <openai>)
| filter type in ("admin_api_key_created", "platform_api_key_created", "api_key.created")
| fields _time, _vendor, actor, api_key_id
| sort desc _time
```

## Tuning

| Variable                                       | Default | Notes |
|------------------------------------------------|---------|---|
| `vendors[v].schedule_minutes`                  | `5`     | Per-vendor poll cadence |
| `vendors[v].initial_lookback_minutes`          | `60`    | First-run window before saved state |
| `OVERLAP_SECONDS` (code)                       | `300`   | Re-query margin for clock skew |
| `MAX_RECENT_IDS` (code)                        | `10000` | Bound on dedupe state size |
| `bucket_object_retention_days` (AWS)           | `365`   | S3 lifecycle (Anthropic-side retention is 6yr) |
| `subscription_message_retention_seconds` (GCP) | `604800` | 7-day buffer if XSIAM is down |
| `ANTHROPIC_COMPLIANCE_API_PATH` (env)          | `/v1/compliance/activities` | Override for spec revisions |
| `OPENAI_AUDIT_LOGS_PATH` (env)                 | `/v1/organization/audit_logs` | Override for spec revisions |

## Operational notes

- **First run** with no saved state pulls only `initial_lookback_minutes` per vendor.
- **Failure modes:**
  - Egress error: aborts before watermark advance; next tick replays the same window. Dedupe by `id` handles overlap.
  - Anthropic 401/403: error message points at Compliance API enablement + key scope.
  - OpenAI 401/403: error message points at *Audit logging* setting + admin-key requirement.
  - Either 404: error message names the documented path and the env-var override.
  - Either 400: structured error message surfaced verbatim.
- **Self-audit loop:** the forwarder's own access shows up as `compliance_api_accessed` (Anthropic) and as a logged request to `/v1/organization/audit_logs` (OpenAI). Useful for the SOC to detect anomalous forwarder activity (or absence thereof).
- **Cost:** at the default 5-min cadence with low audit volume, AWS and GCP free tiers cover both vendors.

## Falling back to the HTTP Collector path

`src/forwarder/egress/http.py` POSTs directly to an XSIAM HTTP Collector. Swap the egress instance in your handler. Caveats: the auth header name and gzip support aren't authoritatively documented by Palo for the HTTP Collector — verify against your tenant's collector config screen. The native paths avoid these unknowns.

## References

- **Anthropic**
  - [Compliance API access guide](https://support.claude.com/en/articles/13015708-access-the-compliance-api)
  - [Compliance API announcement](https://claude.com/blog/claude-platform-compliance-api)
  - [Admin API overview](https://platform.claude.com/docs/en/build-with-claude/administration-api)
  - [Cowork OpenTelemetry](https://support.claude.com/en/articles/14477985-monitor-claude-cowork-activity-with-opentelemetry)
- **OpenAI**
  - [Admin and Audit Logs API help center](https://help.openai.com/en/articles/9687866-admin-and-audit-logs-api-for-the-api-platform)
  - [Audit Logs API reference](https://platform.openai.com/docs/api-reference/audit-logs)
  - [Compliance Platform for Enterprise](https://help.openai.com/en/articles/9261474-compliance-apis-for-enterprise-customers)
- **Palo Alto / Cortex XSIAM**
  - [External log sources overview](https://docs-cortex.paloaltonetworks.com/r/Cortex-XSIAM/Cortex-XSIAM-Documentation/Visibility-of-logs-and-alerts-from-external-sources)
  - [Ingest from GCP Pub/Sub](https://docs-cortex.paloaltonetworks.com/r/Cortex-XSIAM/Cortex-XSIAM-Documentation/Ingest-Logs-and-Data-from-a-GCP-Pub/Sub)
  - [Ingest generic logs from S3](https://docs-cortex.paloaltonetworks.com/r/Cortex-XSIAM/Cortex-XSIAM-Documentation/Ingest-generic-logs-from-Amazon-S3)
  - [PaloAltoNetworks/terraform-umbrella-s3-to-xsiam-ingestion-module](https://github.com/PaloAltoNetworks/terraform-umbrella-s3-to-xsiam-ingestion-module) (reference architecture)
- **AWS**
  - [Lambda runtimes](https://docs.aws.amazon.com/lambda/latest/dg/lambda-runtimes.html)
- **GCP**
  - [Cloud Functions Python runtime](https://cloud.google.com/functions/docs/concepts/python-runtime)
