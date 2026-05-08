# Documentation index

| Doc | Audience | What's in it |
|---|---|---|
| [architecture.md](architecture.md) | Engineers reading or extending the codebase | Vendor-adapter pattern, idempotency model, parallel-execution guarantees, dataflow diagrams (per cloud), failure semantics |
| [coverage.md](coverage.md) | Compliance / SOC charter owners | Full coverage matrix, per-feed event-type inventories, coverage gaps, volume profile |
| [deployment-aws.md](deployment-aws.md) | Operators deploying on AWS | Prerequisites, Terraform apply, XSIAM data source onboarding, outputs reference, common errors |
| [deployment-gcp.md](deployment-gcp.md) | Operators deploying on GCP | Same shape as AWS doc, GCP-specific |
| [operations.md](operations.md) | On-call SREs | Runbook (key rotation, backfill, alarms, recovery), cost dimensions, on-call playbook |
| [security.md](security.md) | Security architects | Threat model, IAM scopes (least privilege), cross-account assume-role pattern, data classification, network exposure |
| [vendors/anthropic.md](vendors/anthropic.md) | Anthropic-specific setup | Compliance API enablement, Activity Feed + chat content, key types, Cowork OTel pairing |
| [vendors/openai.md](vendors/openai.md) | OpenAI-specific setup | Audit Logs setup, Conversations skeleton notes, Palo Alto native integration alternative |
| [xsiam-integration.md](xsiam-integration.md) | XSIAM operators | Data source onboarding per ingestion path, parser hints per feed, XQL recipe library, cross-feed correlation |

For the standalone Cowork OTel collector, see
[../cowork-otel/README.md](../cowork-otel/README.md).

## Quick navigation

- **First-time deploying?** → [deployment-aws.md](deployment-aws.md) or
  [deployment-gcp.md](deployment-gcp.md), then
  [xsiam-integration.md](xsiam-integration.md).
- **Reviewing security before deploy?** →
  [security.md](security.md) → [coverage.md](coverage.md).
- **On-call paged?** → [operations.md](operations.md#on-call-playbook).
- **SOC asking what we capture?** → [coverage.md](coverage.md#at-a-glance).
- **Engineer asking how it works?** → [architecture.md](architecture.md).
- **Adding a new vendor?** →
  [architecture.md § vendor-adapter pattern](architecture.md#vendor-adapter-pattern).
