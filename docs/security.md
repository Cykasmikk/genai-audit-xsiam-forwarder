# Security

Threat model, trust boundaries, and IAM scopes for the forwarder.
Audience: security architects reviewing the design before deployment.

## Trust boundaries

```
       ┌──────────────────────────────────────────────────────────┐
       │                  CUSTOMER CLOUD ACCOUNT                  │
       │                                                          │
       │  ┌────────────┐    ┌────────────┐    ┌────────────────┐  │
       │  │  Lambda /  │    │ DynamoDB / │    │   Audit S3 /   │  │
       │  │ Cloud Func │───▶│  Firestore │    │  Pub/Sub topic │  │
       │  └─────┬──────┘    │  (state)   │    └────────┬───────┘  │
       │        │           └────────────┘             │          │
       │        │ HTTPS                                │ pulled   │
       │        │ TLS 1.2+                             │ via      │
       │        │                                      │ assumed  │
       │        ▼                                      │ role +   │
       │  ┌────────────┐                               │ ext_id / │
       │  │ Secrets    │                               │ SA cred  │
       │  │ Mgr        │                               │          │
       │  │ (api keys) │                               ▼          │
       │  └────────────┘                  ┌─────────────────────┐ │
       │                                  │      XSIAM TENANT   │ │
       │                                  │   (separate AWS /   │ │
       │                                  │    GCP boundary)    │ │
       │                                  └─────────────────────┘ │
       └────────┬─────────────────────────────────────────────────┘
                │ HTTPS to api.anthropic.com / api.openai.com
                │ TLS-pinned by SDK
                ▼
       ┌──────────────────────┐
       │   VENDOR (Anthropic  │
       │   / OpenAI) PLATFORM │
       └──────────────────────┘
```

For the Cowork OTel collector add a third inbound boundary:

```
   Anthropic Cowork backend  ─┐
                              ├─ HTTPS bearer-auth ─▶  Collector ─▶ S3 / Pub/Sub
   Developer workstations    ─┘     (public ingress)
```

## Threat model

| Threat | Mitigation |
|---|---|
| API key leakage from Lambda env vars | Keys stored in Secrets Manager / Secret Manager, never as Lambda env vars. The Lambda's IAM role grants `secretsmanager:GetSecretValue` only on its own feed's secret. |
| State tampering causing data loss | DynamoDB has point-in-time recovery enabled (35-day rolling). Firestore retention is per project policy. State write is per-vendor PK; no cross-vendor write authority. |
| Cross-feed data leakage in S3 | IAM policy on each Lambda role grants `s3:PutObject` only on its own `{vendor}/*` prefix. XSIAM's role grants `s3:GetObject` per-vendor too — even if XSIAM's role were compromised, it couldn't read other clouds' audit data. |
| Compromised XSIAM tenant exfiltrating audit data | Cross-account IAM role assumed by a known XSIAM AWS account ID with a random UUIDv4 external ID; SA on GCP scoped to subscription-only access. The blast radius is **audit data** that XSIAM is already supposed to see — no broader access. |
| Compromised forwarder account writing fake audit events to XSIAM | Lambda's IAM role can write to S3 / publish to Pub/Sub but **not modify the bucket policy or topic ACL**. An attacker with Lambda execution access could inject events but not silence the real audit feed (the vendor APIs continue to emit events). XSIAM-side ingestion authenticity is bounded by the cross-account IAM role + external ID. |
| Vendor API DNS hijack | TLS cert validation by the boto3 / google-cloud-pubsub SDK. We do not pin a specific cert (we'd lose vendor cert-rotation tolerance). |
| Cowork OTel bearer token leakage | Token is 48-char random, stored in Secrets Manager / Secret Manager. Rotate via `terraform apply -replace=random_password.bearer_token`. Anthropic admin portal encrypts it at rest. Claude Code managed-settings rollouts deliver it via OTEL_EXPORTER_OTLP_HEADERS — same risk as any IT-managed credential. |
| Cowork OTel public ingress abuse | Bearer-token auth on the OTLP HTTP listener; no other route is exposed. AWS path can add a WAF layer if needed; GCP Cloud Run has its own DDoS protections. The collector is stateless — flooding it with junk would cost money but not breach data. |

## IAM scopes

### AWS — per-vendor Lambda role (one per feed)

```
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "Logs",
      "Effect": "Allow",
      "Action": ["logs:CreateLogGroup","logs:CreateLogStream","logs:PutLogEvents"],
      "Resource": "arn:aws:logs:<region>:<account>:*"
    },
    {
      "Sid": "State",
      "Effect": "Allow",
      "Action": ["dynamodb:GetItem","dynamodb:PutItem"],
      "Resource": "<state table arn>"
    },
    {
      "Sid": "Secret",
      "Effect": "Allow",
      "Action": "secretsmanager:GetSecretValue",
      "Resource": "<this feed's secret arn>"      // not other feeds'
    },
    {
      "Sid": "WriteAuditObjects",
      "Effect": "Allow",
      "Action": "s3:PutObject",
      "Resource": "<bucket>/<vendor>/*"           // only this feed's prefix
    }
  ]
}
```

The state table is shared across feeds (one item per vendor PK), but
the IAM policy is the same for every feed because we cannot scope
DynamoDB IAM by item PK. **A compromised Lambda for feed X could
overwrite the state row for feed Y.** This is acceptable in the threat
model because:
- The state row contains no secrets, only a watermark + ID set.
- An overwrite causes feed Y to replay or skip a window of events,
  not lose them permanently (the vendor retains them).
- Any Lambda compromise is already a critical incident; this is not
  the most severe consequence.

If your threat model requires per-feed state isolation, change
`DynamoStateStore` to use one table per vendor (one-line change to the
table name in the constructor) and add per-feed `aws_dynamodb_table`
resources in Terraform.

### AWS — cross-account XSIAM role

```
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ReadAuditObjects",
      "Effect": "Allow",
      "Action": "s3:GetObject",
      "Resource": [
        "<bucket>/anthropic/*",
        "<bucket>/anthropic_chats/*",
        "<bucket>/openai/*",
        "<bucket>/openai_conversations/*"
      ]
    },
    {
      "Sid": "ListBucket",
      "Effect": "Allow",
      "Action": ["s3:ListBucket","s3:GetBucketLocation"],
      "Resource": "<bucket>",
      "Condition": {
        "StringLike": {
          "s3:prefix": ["anthropic/*","anthropic_chats/*","openai/*","openai_conversations/*"]
        }
      }
    },
    {
      "Sid": "ConsumeNotifications",
      "Effect": "Allow",
      "Action": ["sqs:ReceiveMessage","sqs:DeleteMessage","sqs:GetQueueAttributes","sqs:GetQueueUrl"],
      "Resource": ["<sqs queue arn per feed>"]
    }
  ]
}
```

Trust policy:
```
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": { "AWS": "arn:aws:iam::<XSIAM tenant account>:root" },
    "Action": "sts:AssumeRole",
    "Condition": { "StringEquals": { "sts:ExternalId": "<random uuid v4>" } }
  }]
}
```

External ID is generated by `random_uuid` in Terraform and surfaced as
a sensitive output. Rotate by `terraform apply -replace=random_uuid.xsiam_external_id`
and re-paste into the XSIAM data source.

### GCP — per-vendor function SA

Each feed has a dedicated service account with three role bindings:

- `roles/secretmanager.secretAccessor` on its own API-key secret only
- `roles/datastore.user` (project-scoped) — Firestore can't be scoped by
  document path
- `roles/pubsub.publisher` on its own audit topic only

The Datastore-scope concern is the same as DynamoDB above — accept the
risk or split into per-feed databases.

### GCP — XSIAM SA

One SA shared across feeds, with `roles/pubsub.subscriber` per
subscription and `roles/pubsub.viewer` per topic. No project-level
roles.

The JSON key is **not** created by Terraform (keys in TF state are an
audit smell). Operator generates it out-of-band:

```bash
gcloud iam service-accounts keys create xsiam.json \
  --iam-account=$(terraform output -raw xsiam_service_account_email)
```

Pastes into XSIAM data source config, then deletes the local file.
Rotate by `gcloud iam service-accounts keys delete <id>` + create new.

## Data classification

| Feed | What's in the payload | Classification (typical) |
|---|---|---|
| `anthropic` | User emails, IP addresses, user agents, admin actions, file IDs (no content) | Internal — operational metadata |
| `anthropic_chats` | **Full chat transcripts including any data users typed into Claude.ai**, file IDs (and binary content if `ANTHROPIC_FETCH_FILE_CONTENT=1`) | Restricted — may contain regulated data (PHI, PCI, secrets, etc.) |
| `openai` | User emails, IP addresses, admin actions, project IDs | Internal |
| `openai_conversations` | **Full ChatGPT conversation transcripts** | Restricted |
| Cowork OTel | **User prompts**, tool parameters, file paths accessed, model + cost | Restricted |

For Restricted feeds, consider:

- **Separate XSIAM dataset** with stricter access control (limit to
  authorized incident responders, not the broader SOC team).
- **Field-level redaction at ingest** via XSIAM parsing rules — strip
  obvious patterns (CCN, SSN, secrets) from `message.content` before
  indexing.
- **Field-level redaction upstream** via OTel collector processors:
  the [OTel `attributesprocessor`](https://github.com/open-telemetry/opentelemetry-collector-contrib/tree/main/processor/attributesprocessor)
  supports regex deletion. Add a stage to `collector-config.yaml.tftpl`
  if your policy requires it.
- **Shorter retention** on the Restricted dataset (e.g. 90 days) than
  on Internal datasets (1 year).

## Network exposure

| Component | Inbound | Outbound |
|---|---|---|
| Polling Lambda / Cloud Function | None (invoked only by EventBridge / Cloud Scheduler) | `api.anthropic.com:443`, `api.openai.com:443`, S3 / Pub-Sub control plane, Secrets Manager / Secret Manager, DynamoDB / Firestore |
| Cowork OTel collector (AWS) | Public HTTPS 443 → ALB → 4318 | S3 control plane, Secrets Manager |
| Cowork OTel collector (GCP) | Public HTTPS 443 → Cloud Run | Pub/Sub control plane, Secret Manager |
| XSIAM tenant | (cross-account) | (cross-account) |

The polling forwarders make outbound HTTPS only — they have no inbound
exposure beyond the cloud-managed scheduler. The Cowork OTel collector
is the only public-facing component because the Cowork backend and
developer-workstation Claude Code agents need to reach it; bearer-token
auth on `/v1/logs` is the access control.

## Logging and audit of the forwarder itself

Anthropic's `compliance_api_accessed` event type is emitted for every
Compliance API request (including ours). This means:

- The forwarder appears in the very feed it is forwarding.
- An anomaly detection on `compliance_api_accessed` events from an
  unexpected IP, user agent, or absent for an unexpected duration
  catches forwarder compromise or downtime.
- Recommended XQL alert: `compliance_api_accessed` events from any IP
  *not* in the allow-list of forwarder Lambda VPC NAT gateway IPs (or
  AWS Lambda's documented egress IP ranges).

OpenAI's Audit Logs API also logs all authenticated requests — same
self-audit story.

CloudWatch / Cloud Logging captures every Lambda / Function invocation;
errors surface immediately. See [docs/operations.md](operations.md#alarms)
for recommended alarm policies.

## Compliance notes

- **Encryption in transit**: All vendor-API and XSIAM connections are
  TLS 1.2+; ALB / Cloud Run terminate TLS 1.2+ for the OTel collector.
- **Encryption at rest**: S3 bucket has AES-256 default encryption;
  Pub/Sub uses Google-managed encryption; DynamoDB has SSE enabled;
  Firestore uses Google-managed encryption; secrets in Secrets Manager
  / Secret Manager are encrypted with KMS.
- **Audit trail of changes**: All Terraform changes are diffable in
  Git; CI runs `terraform validate` per PR. Recommend code review on
  every PR that touches `terraform/` or `cowork-otel/terraform/`.
- **Retention**:
  - Anthropic Activity Feed: 6 years upstream.
  - OpenAI Audit Logs: per OpenAI data retention policy.
  - S3 audit objects: `bucket_object_retention_days` Terraform var,
    default 365.
  - Pub/Sub subscription retention: 7 days max (Pub/Sub limit) — XSIAM
    must consume before that.
  - DynamoDB / Firestore state: until manually deleted.
