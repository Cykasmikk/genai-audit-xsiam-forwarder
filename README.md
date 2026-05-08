# genai-audit-xsiam-forwarder

[![ci](https://github.com/Cykasmikk/claude-to-xsiam/actions/workflows/ci.yml/badge.svg)](https://github.com/Cykasmikk/claude-to-xsiam/actions/workflows/ci.yml)
[![license](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

Forwards GenAI platform audit logs **and conversation content** into
**Cortex XSIAM** using the cloud-native ingestion patterns documented by
Palo Alto Networks. Vendor-adapter architecture — drop in a new adapter
to add another provider.

**Five feeds across two vendors plus an OpenTelemetry collector for
inference visibility:**

| Feed | What it captures | Status |
|---|---|---|
| `anthropic` | Anthropic Compliance API Activity Feed (~200 admin/auth/resource event types) | ✅ Production (Rev J 2026-04-20) |
| `anthropic_chats` | Full Claude.ai chat transcripts (prompts, responses, files) | ✅ Production (Rev J) |
| `openai` | OpenAI Audit Logs (51 admin/auth/project event types) | ✅ Production |
| `openai_conversations` | Full ChatGPT Enterprise/Edu conversation transcripts | ⚠️ Skeleton — endpoint not publicly documented; see [docs/coverage.md](docs/coverage.md#coverage-gaps) |
| Cowork OTel | Claude Code + Cowork prompts, tool calls, file access, model + token + cost per request | ✅ Production |

> **Designed for full-take SOC ingestion.** Audit metadata, chat
> transcripts, and inference telemetry — every signal each vendor
> exposes, paginated to exhaustion, with no event-type filter applied.
> See [docs/coverage.md](docs/coverage.md) for the complete inventory and
> known gaps.

## Architecture at a glance

```
                    ┌──────────────────────────────────────┐
                    │         Cortex XSIAM                 │
                    │  (one data source per feed, datasets │
                    │   partitioned by vendor / content)   │
                    └─────────────▲────────────────────────┘
                                  │ pulls native S3+SQS / Pub-Sub
                                  │
        ┌─────────────────────────┴────────────────────────┐
        │                                                  │
   ┌────▼─────────────┐                          ┌─────────▼──────┐
   │ Polling fan-out  │                          │ Push collector │
   │  (Lambda × N /   │                          │  (ECS Fargate /│
   │   Function × N)  │                          │   Cloud Run)   │
   │                  │                          │                │
   │ anthropic        │                          │ OTLP HTTP      │
   │ anthropic_chats  │                          │ + bearer auth  │
   │ openai           │                          │                │
   │ openai_convs     │                          │                │
   └────▲─────────────┘                          └─────▲──────────┘
        │ paginated audit/content                      │ OTLP push
        │                                              │
   ┌────┴─────────────┐                          ┌─────┴──────────┐
   │ Anthropic /      │                          │ Cowork backend │
   │ OpenAI APIs      │                          │ + Claude Code  │
   └──────────────────┘                          └────────────────┘
```

See [docs/architecture.md](docs/architecture.md) for dataflow diagrams,
the vendor-adapter pattern, and parallel-execution guarantees.

## Quickstart

### 1. Prerequisites

- Cortex XSIAM tenant with the data source onboarding screens accessible
- Anthropic Enterprise plan with Compliance API enabled, OpenAI org with
  audit logging enabled (or any subset — feeds are independent)
- Terraform ≥ 1.6 and credentials for AWS or GCP

Detailed prerequisites per cloud are in
[docs/deployment-aws.md](docs/deployment-aws.md) /
[docs/deployment-gcp.md](docs/deployment-gcp.md). Per-vendor setup steps
are in [docs/vendors/anthropic.md](docs/vendors/anthropic.md) /
[docs/vendors/openai.md](docs/vendors/openai.md).

### 2. Deploy the polling forwarders

```bash
cd terraform/aws    # or terraform/gcp
terraform init
terraform apply -var-file=example.tfvars
```

Example `tfvars` for both clouds is in the deployment guides. To deploy
a subset of feeds, omit them from the `vendors` and `api_keys` maps.

### 3. (Optional) Deploy the Cowork OTel collector

For Claude Code and Cowork inference visibility:

```bash
cd cowork-otel/terraform/aws    # or cowork-otel/terraform/gcp
terraform init
terraform apply
```

See [cowork-otel/README.md](cowork-otel/README.md) for the agent-side
configuration the operator pastes into the Anthropic admin portal.

### 4. Wire up XSIAM

Each feed has its own outputs (role ARN + external ID + SQS URL on AWS,
or subscription name + SA email on GCP). Paste these into one XSIAM
data source per feed. Worked examples:
[docs/xsiam-integration.md](docs/xsiam-integration.md).

### 5. Verify

XQL example library across all five feeds — failed-login spikes,
prompt-content DLP regex, cross-vendor API-key creation tracking — is
in [docs/xsiam-integration.md](docs/xsiam-integration.md#xql-recipes).

## Documentation

| Doc | Audience |
|---|---|
| [docs/architecture.md](docs/architecture.md) | Engineers reading or extending the codebase |
| [docs/coverage.md](docs/coverage.md) | Compliance / SOC charter owners — what's in, what's not, why |
| [docs/deployment-aws.md](docs/deployment-aws.md) | Operators deploying on AWS |
| [docs/deployment-gcp.md](docs/deployment-gcp.md) | Operators deploying on GCP |
| [docs/operations.md](docs/operations.md) | On-call SREs — runbook, key rotation, alarms, recovery |
| [docs/security.md](docs/security.md) | Security architects — threat model, IAM scopes, data-flow review |
| [docs/vendors/anthropic.md](docs/vendors/anthropic.md) | Anthropic-specific setup detail |
| [docs/vendors/openai.md](docs/vendors/openai.md) | OpenAI-specific setup detail |
| [docs/xsiam-integration.md](docs/xsiam-integration.md) | XSIAM operators — data source onboarding, XQL recipes |
| [cowork-otel/README.md](cowork-otel/README.md) | Cowork OTel collector deployment |

## Repository layout

```
src/forwarder/        Python forwarder package
  vendors/            Per-vendor adapters (anthropic_compliance,
                      anthropic_chat_content, openai_audit, openai_conversations)
  egress/             Per-cloud sinks (s3, pubsub, http fallback)
  core.py             Vendor-agnostic fetch → forward → checkpoint loop
terraform/aws/        Multi-feed Lambda + S3 + SQS + DynamoDB stack
terraform/gcp/        Multi-feed Cloud Function + Pub/Sub + Firestore stack
cowork-otel/          OTel collector for Claude Code + Cowork (separate stack)
docs/                 Operator documentation (this directory)
tests/smoke.py        47 deterministic tests (no AWS/GCP creds needed)
.github/workflows/    CI — runs smoke + terraform validate per PR
```

## Status & versioning

- Spec conformance:
  - Anthropic Compliance API: **Rev J 2026-04-20** (Activity Feed +
    chat content endpoints fully wired)
  - OpenAI Audit Logs API: latest publicly documented (verified May 2026)
  - OpenAI Conversations API: skeleton — see
    [docs/coverage.md](docs/coverage.md#coverage-gaps)
- CI enforces 47-case smoke suite + `terraform fmt -check` +
  `terraform validate` on every PR.
- License: Apache 2.0.

## Support

This is internal SOC tooling — no commercial support. Issues and PRs
welcome at <https://github.com/Cykasmikk/claude-to-xsiam>.
