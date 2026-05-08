# Cowork OpenTelemetry collector

Receives OTLP push from Anthropic Claude Cowork and Claude Code agents,
forwards to Cortex XSIAM via the same S3 / Pub/Sub pattern the polling
forwarder uses.

This is **not** a poller. The agents push to a public-internet HTTPS
endpoint that you operate. Anthropic configures the endpoint and bearer
token centrally (Cowork) or via env vars on developer workstations
(Claude Code).

## What it captures

Per [Anthropic's Cowork OTel doc](https://support.claude.com/en/articles/14477985-monitor-claude-cowork-activity-with-opentelemetry):

- **Full text of prompts** users submit
- **Tool / MCP invocations** (server name, tool name, parameters,
  success/failure, exec time)
- **File access** paths (read, modified, MCP-mediated)
- **Skills and plugins** invoked
- **Human approval decisions** (approved / rejected / auto-permitted)
- **Per-request:** model, token counts, cost, duration, errors
- **Shared `prompt.id`** linking all events from one user input
- **User email**

For Claude Code on developer workstations, the same data plus
[managed-settings env vars](https://code.claude.com/docs/en/monitoring-usage):

```
CLAUDE_CODE_ENABLE_TELEMETRY=1
OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
OTEL_EXPORTER_OTLP_ENDPOINT=https://<your-collector-endpoint>
OTEL_EXPORTER_OTLP_HEADERS=Authorization=Bearer <token>
OTEL_LOGS_EXPORTER=otlp
OTEL_METRICS_EXPORTER=otlp
OTEL_LOG_USER_PROMPTS=1     # opt-in: capture prompt text
OTEL_LOG_TOOL_DETAILS=1     # opt-in: capture tool args
```

## Architecture

```
   Anthropic Cowork backend  ─┐
   Claude Code workstations   ├─▶  HTTPS bearer-auth  ─▶  OTel Collector (this repo)
   ...                        ─┘                                 │
                                                                 │ awss3 / googlecloudpubsub exporter
                                                                 ▼
                                                       ┌─────────────────────┐
                                                       │ S3 / Pub/Sub        │
                                                       │ under cowork/ prefix│
                                                       └──────────┬──────────┘
                                                                  │ XSIAM polls
                                                                  ▼
                                                        ┌──────────────────┐
                                                        │   Cortex XSIAM   │
                                                        └──────────────────┘
```

## Why two terraform stacks

Same as the polling forwarder: pick AWS or GCP based on where your
SOC's primary egress lives.

- **`terraform/aws/`** deploys ECS Fargate + ALB (HTTPS) + Secrets
  Manager (bearer token) + IAM role with S3 write to a Cowork prefix
  on the shared audit bucket.

- **`terraform/gcp/`** deploys Cloud Run (HTTPS native) + Secret Manager
  (bearer token) + dedicated SA with pubsub.publisher on a Cowork topic.

Both reuse the audit bucket / state table / XSIAM IAM role from the
parent `terraform/aws|gcp/` stack — call `terraform output` there first
to get the references.

## Prerequisites

### AWS

- VPC with ≥2 public subnets in different AZs (for the ALB)
- ACM certificate for the hostname you want to put on the collector
  (e.g. `cowork-otel.soc.example.com`)
- The shared audit bucket from the parent Terraform stack (create that
  first via `terraform/aws/`)

### GCP

- Cloud Run is regional and HTTPS by default; no extra networking
  required
- The shared XSIAM IAM/SA from the parent `terraform/gcp/` stack
- A Cowork-specific Pub/Sub topic (created by this stack)

## Deploy — AWS

```bash
cd cowork-otel/terraform/aws
terraform init
terraform apply \
  -var "audit_bucket=<output from parent stack>" \
  -var "xsiam_role_name=<output from parent stack>" \
  -var "vpc_id=vpc-xxx" \
  -var 'public_subnet_ids=["subnet-aaa","subnet-bbb"]' \
  -var "acm_certificate_arn=arn:aws:acm:..." \
  -var "hostname=cowork-otel.soc.example.com"
```

After `apply`:

1. Get the bearer token: `terraform output -raw bearer_token`.
2. Get the ALB hostname: `terraform output -raw collector_endpoint`.
3. Configure Cowork: *Anthropic admin portal → Organization settings
   → Cowork → OTLP endpoint*: `https://<hostname>/v1/logs` (the OTLP
   HTTP path); add header `Authorization: Bearer <token>`.
4. Configure Claude Code via managed settings — see the env-var snippet
   above.
5. New objects appear in `s3://<audit_bucket>/cowork/...`. Configure an
   XSIAM *Amazon S3 generic logs* data source against the SQS queue
   created by this stack (separate from the audit-feed queues).

## Deploy — GCP

```bash
cd cowork-otel/terraform/gcp
terraform init
terraform apply \
  -var "project_id=my-soc-project" \
  -var "region=us-central1" \
  -var "xsiam_service_account_email=<output from parent stack>"
```

After `apply`:

1. Get the bearer token: `terraform output -raw bearer_token`.
2. Get the Cloud Run URL: `terraform output -raw collector_endpoint`.
3. Configure Cowork (same as AWS step 3 above).
4. Configure Claude Code (same env vars).
5. Pub/Sub topic and subscription are created. Configure an XSIAM
   *GCP Pub/Sub* data source pointing at the new subscription using the
   same XSIAM SA credential file you generated for the audit feeds.

## Operational notes

- **OTLP path** is `/v1/logs` for the HTTP/protobuf protocol. Cowork and
  Claude Code both use OTel logs as the signal type for prompt/event
  data; metrics live on `/v1/metrics` if you want token-count graphs in
  XSIAM too (collector forwards both pipelines).
- **Bearer token rotation** — `terraform apply -replace random_password.token`
  generates a new one. Re-paste into the Anthropic admin portal and
  managed-settings rollout.
- **High availability** — AWS stack runs 2 Fargate tasks behind the ALB
  by default. Cloud Run scales 1–10 instances by default.
- **Cost** — Cowork emits per-request events. At organization scale
  (hundreds of seats × dozens of prompts/day) this is much higher
  volume than the audit feeds. SOC retention should account for it.

## Security

- The collector accepts traffic from the public internet because that's
  where Anthropic's Cowork backend and developer workstations connect
  from. Bearer-token auth on `/v1/logs` is the only access control. If
  you need IP allowlisting, add a WAF (AWS) or VPC connector rule
  (GCP). Token-encryption-at-rest is handled by Anthropic's admin
  portal per their docs.
- The collector itself does not retain payloads — it forwards
  immediately to S3/Pub-Sub and discards local state.
- Sample collector logs *do not* include payload bodies; only counts
  and error messages.
