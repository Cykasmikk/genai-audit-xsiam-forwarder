# claude-xsiam-log-forwarder

Forwards **Claude Platform Compliance API** audit events into **Cortex XSIAM**
using the cloud-native ingestion patterns documented and reference-architected
by Palo Alto Networks:

- **AWS:** Lambda → S3 (gzipped JSON-lines) → S3 ObjectCreated → SQS → XSIAM
  pulls via cross-account IAM role with external ID. Mirrors the Palo-published
  [`terraform-umbrella-s3-to-xsiam-ingestion-module`](https://github.com/PaloAltoNetworks/terraform-umbrella-s3-to-xsiam-ingestion-module).
- **GCP:** Cloud Function → Pub/Sub topic → XSIAM pulls via dedicated pull
  subscription with a service-account credential file.

A direct HTTP-Collector path is included as a non-default fallback.

There is no native Anthropic integration in XSIAM, and no Anthropic-published
XSIAM connector. This repo is the custom forwarder.

## What this captures vs. what it does not

The Compliance API records **admin and system events** (sign-in, key creation,
workspace membership changes, SSO config, settings changes) and **resource
activity** (file upload/download/deletion, skill creation, etc.). It does
**not** include inference activity — i.e. user prompts, model responses, or
tool-call payloads.

For inference visibility, deploy [Claude Cowork OpenTelemetry](https://support.claude.com/en/articles/14477985-monitor-claude-cowork-activity-with-opentelemetry)
as a sibling pipeline. The two feeds correlate via a shared user account
identifier present in every Cowork OTel event.

## Architecture

### AWS — native (default)

```
       every 5 min
   ┌─────────────┐    ┌──────────────┐   PutObject    ┌────────────┐
   │ EventBridge │ ─▶ │   Lambda     │ ─────────────▶ │ S3 audit   │
   └─────────────┘    │  forwarder   │                │   bucket   │
                     └──────┬────────┘                └─────┬──────┘
                            │ Compliance API                │ ObjectCreated
                            ▼                               ▼
                     ┌──────────────┐                ┌──────────────┐
                     │ api.anthropic│                │  SQS queue   │
                     │ .com /v1/orgs│                └──────┬───────┘
                     └──────────────┘                       │ XSIAM polls
                                                            ▼
                                                  ┌──────────────────┐
                                                  │  Cortex XSIAM    │
                                                  │   (assumed role  │
                                                  │   + external ID) │
                                                  └──────────────────┘
```

### GCP — native (default)

```
       every 5 min
   ┌─────────────┐    ┌────────────────┐    publish    ┌────────────────┐
   │  Scheduler  │ ─▶ │ Cloud Function │ ────────────▶ │  audit topic   │
   └─────────────┘    │   forwarder    │               └────────┬───────┘
                     └──────┬──────────┘                        │
                            │ Compliance API                    ▼
                            ▼                          ┌────────────────┐
                     ┌──────────────┐                  │ XSIAM-bound    │
                     │ api.anthropic│                  │  subscription  │
                     │ .com /v1/orgs│                  └────────┬───────┘
                     └──────────────┘                           │ XSIAM pulls
                                                                ▼
                                                      ┌──────────────────┐
                                                      │  Cortex XSIAM    │
                                                      │  (SA credential  │
                                                      │   JSON file)     │
                                                      └──────────────────┘
```

### Idempotency model

The publicly documented Compliance API event schema does not include a stable
event-id field, so we cannot cursor-by-id. Each tick:

1. Loads the prior **watermark** (latest `created_at` ever forwarded) and a
   bounded set of **recent content hashes** (SHA-256 of canonical event JSON).
2. Queries the API window `[watermark - 5min, now]` to absorb clock skew and
   late-arriving events near the boundary.
3. Drops events whose content hash is already in `recent_hashes`.
4. Forwards the survivors to the configured egress sink.
5. Persists the advanced watermark + refreshed hash set **only after** the
   egress sink ACKs. A crash mid-batch replays cleanly on the next tick.

XSIAM-side dedupe is recommended as defense-in-depth — the content hash is
stable and projectable as a join/dedupe key in your XQL.

## Prerequisites

1. **Claude Enterprise plan.** Compliance API is GA on Enterprise (excluding
   Public Sector orgs).
2. **Compliance API enabled** by an Enterprise Primary Owner under
   *Organization settings → Data and Privacy → Compliance API → Enable*.
3. **Admin API key** with Compliance scope (`sk-ant-admin01-...`).
4. **XSIAM data source pre-info** — depends on which path:
   - **AWS path:** the AWS account ID of your XSIAM tenant. This is shown in
     the *Settings → Data Sources → Add → Amazon S3 generic logs* onboarding
     screen. You'll paste the role ARN, external ID, and SQS URL produced by
     this Terraform back into that screen after `apply`.
   - **GCP path:** none in advance. After `apply` you generate a SA key and
     paste it (along with the subscription name) into the *GCP Pub/Sub* data
     source onboarding screen.
   - **HTTP fallback:** an HTTP Collector configured as a Custom App, with
     its tenant URL and auth token.
5. **Compliance API spec PDF** from your Anthropic account team — see
   "Spec verification checklist" below.
6. Terraform ≥ 1.6.

## Spec verification checklist (DO BEFORE PROD DEPLOY)

The exact endpoint path, time-filter parameter names, and pagination tokens
of the Compliance API are not on any public Anthropic page — they're in a
PDF available only to customers with the Compliance API enabled. The client
in `src/forwarder/claude_client.py` ships with **educated defaults aligned
with Anthropic's sibling Usage/Cost Admin API** (the closest public Admin API
spec), marked with `TODO(compliance-pdf)` comments. Verify each against your
PDF and override:

| Item                        | Default                            | Override                              |
|-----------------------------|------------------------------------|---------------------------------------|
| Endpoint path               | `/v1/organizations/audit_logs`     | `COMPLIANCE_API_PATH` env var         |
| Time filter param (start)   | `starting_at`                      | `PARAM_STARTING_AT` constant          |
| Time filter param (end)     | `ending_at`                        | `PARAM_ENDING_AT` constant            |
| Pagination token param      | `page`                             | `PARAM_PAGE` constant                 |
| Page size param             | `limit`                            | `PARAM_LIMIT` constant                |
| Response data key           | `data`                             | `RESP_DATA` constant                  |
| Response has-more flag      | `has_more`                         | `RESP_HAS_MORE` constant              |
| Response next-page token    | `next_page`                        | `RESP_NEXT_PAGE` constant             |

The client raises an actionable error on HTTP 404 (path wrong, fix env var)
and on 401/403 (Compliance API not enabled or key lacks scope).

The event-field schema (`created_at`, `actor_info`, `event`, `event_info`,
`entity_info`, `ip_address`, `device_id`, `user_agent`, `client_platform`)
**is** documented at
<https://support.claude.com/en/articles/9970975-access-audit-logs> and is
used as-is.

## Repository layout

```
src/
  main.py                 GCP Cloud Function entrypoint (re-exports handler)
  requirements.txt        GCP Cloud Build installs these at deploy time
  forwarder/
    core.py               fetch → forward → checkpoint loop
    claude_client.py      Compliance API client
    state.py              ForwarderState dataclass + StateStore protocol
    state_aws.py          DynamoDB state backend
    state_gcp.py          Firestore state backend
    aws_handler.py        Lambda entrypoint (uses egress.s3)
    gcp_handler.py        Cloud Function handler (uses egress.pubsub)
    egress/
      __init__.py         Egress protocol
      s3.py               AWS native: gzipped JSON-lines to S3
      pubsub.py           GCP native: publish to Pub/Sub topic
      http.py             Fallback: direct POST to XSIAM HTTP Collector
terraform/aws/            Lambda + EventBridge + S3 + SQS + cross-account
                          IAM role + DynamoDB + Secrets Manager
terraform/gcp/            Cloud Function + Scheduler + audit Pub/Sub topic
                          + XSIAM-bound subscription + SA + Firestore +
                          Secret Manager
```

## Deploy — AWS

```bash
cd terraform/aws
terraform init
terraform apply \
  -var "anthropic_admin_api_key=sk-ant-admin01-..." \
  -var "xsiam_aws_account_id=<XSIAM tenant AWS account ID from XSIAM UI>"
```

After `apply`, paste these outputs into the XSIAM *Amazon S3 generic logs*
data source onboarding screen:

| XSIAM field    | Terraform output       |
|----------------|------------------------|
| Role ARN       | `xsiam_role_arn`       |
| External ID    | `xsiam_external_id`    |
| SQS queue URL  | `xsiam_sqs_url`        |
| Bucket         | `audit_bucket`         |

Get the external ID with:
```bash
terraform output -raw xsiam_external_id
```

## Deploy — GCP

```bash
cd terraform/gcp
terraform init
terraform apply \
  -var "project_id=my-soc-project" \
  -var "region=us-central1" \
  -var "anthropic_admin_api_key=sk-ant-admin01-..."
```

After `apply`:

1. Generate a JSON key for the XSIAM service account (intentionally **not**
   created by Terraform — keys in TF state are an audit smell):

   ```bash
   gcloud iam service-accounts keys create xsiam-credentials.json \
     --iam-account=$(terraform output -raw xsiam_service_account_email)
   ```

2. In the XSIAM *GCP Pub/Sub* data source onboarding screen, paste:

   | XSIAM field        | Source                                  |
   |--------------------|-----------------------------------------|
   | Subscription name  | `terraform output xsiam_audit_subscription` |
   | Service account    | the contents of `xsiam-credentials.json`    |

3. Delete the local key file once XSIAM has it.

## Verifying ingestion in XSIAM

After the first scheduled run:

```xql
dataset = <your_audit_dataset>
| filter event_info or event   // schema fields from the Compliance API
| sort desc _time
| limit 50
```

You should see one row per audit event (sign-in, key creation, workspace
membership change, SSO config update, etc.). The full event payload includes
`actor_info`, `event_info`, `entity_info`, `ip_address`, `device_id`,
`user_agent`, `client_platform`.

## Tuning

| Variable                           | Default | Notes                                              |
|------------------------------------|---------|----------------------------------------------------|
| `schedule_minutes`                 | `5`     | Poll cadence. ~5 min freshness SLO on the API.     |
| `initial_lookback_minutes`         | `60`    | First-run window; later runs use saved state.      |
| `OVERLAP_SECONDS` (code)           | `300`   | Re-query margin for clock skew at boundary.        |
| `MAX_RECENT_HASHES` (code)         | `10000` | Bound on dedupe state size (≈640 KB).              |
| `bucket_object_retention_days` (AWS) | `365` | S3 lifecycle expiry. Set 0 to disable.             |
| `subscription_message_retention_seconds` (GCP) | `604800` | Subscription buffer if XSIAM is down. |

## Operational notes

- **First run** with no saved state pulls only `initial_lookback_minutes` so
  you don't accidentally backfill ~180 days of events into XSIAM (the
  Compliance API's server-side retention).
- **Failure mode (egress)**: any error from the egress sink aborts before the
  watermark advances; the next tick replays the same window. Dedupe handles
  the overlap.
- **Failure mode (Anthropic)**: 401/403 raises with explicit guidance to
  verify Compliance API enablement and key scope. 404 prompts to verify the
  endpoint path against the spec PDF and override `COMPLIANCE_API_PATH`.
- **Cost:** at the default 5-min cadence, AWS and GCP free tiers cover this.

## Falling back to the HTTP Collector path

If you can't (or don't want to) use the native S3/Pub-Sub paths, the
`src/forwarder/egress/http.py` sink POSTs directly to an XSIAM HTTP
Collector. To use it, swap the egress instance in your handler:

```python
# in aws_handler.py or gcp_handler.py
from .egress.http import HttpEgress, HttpEgressConfig

egress = HttpEgress(HttpEgressConfig(
    url=os.environ["XSIAM_COLLECTOR_URL"],
    token=_secret(os.environ["XSIAM_TOKEN_SECRET_ARN"]),
))
```

Caveats: the auth header name and gzip support are not authoritatively
documented by Palo for the HTTP Collector — verify against your tenant's
collector configuration screen. The native paths avoid these unknowns.

## References

- [Compliance API access](https://support.claude.com/en/articles/13015708-access-the-compliance-api)
- [Compliance API announcement](https://claude.com/blog/claude-platform-compliance-api)
- [Audit log fields](https://support.claude.com/en/articles/9970975-access-audit-logs)
- [Usage and Cost API (sibling, public spec reference)](https://platform.claude.com/docs/en/build-with-claude/usage-cost-api)
- [Cowork OpenTelemetry](https://support.claude.com/en/articles/14477985-monitor-claude-cowork-activity-with-opentelemetry)
- [Cortex XSIAM — Visibility of logs and alerts from external sources](https://docs-cortex.paloaltonetworks.com/r/Cortex-XSIAM/Cortex-XSIAM-Documentation/Visibility-of-logs-and-alerts-from-external-sources)
- [Cortex XSIAM — Ingest Logs and Data from a GCP Pub/Sub](https://docs-cortex.paloaltonetworks.com/r/Cortex-XSIAM/Cortex-XSIAM-Documentation/Ingest-Logs-and-Data-from-a-GCP-Pub/Sub)
- [Cortex XSIAM — Ingest generic logs from Amazon S3](https://docs-cortex.paloaltonetworks.com/r/Cortex-XSIAM/Cortex-XSIAM-Documentation/Ingest-generic-logs-from-Amazon-S3)
- [Cortex XSIAM — Ingest logs from Amazon CloudWatch](https://docs-cortex.paloaltonetworks.com/r/Cortex-XSIAM/Cortex-XSIAM-Documentation/Ingest-logs-from-Amazon-CloudWatch)
- [PaloAltoNetworks/terraform-umbrella-s3-to-xsiam-ingestion-module (reference architecture)](https://github.com/PaloAltoNetworks/terraform-umbrella-s3-to-xsiam-ingestion-module)
