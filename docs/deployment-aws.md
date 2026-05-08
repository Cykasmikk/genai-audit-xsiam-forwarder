# Deployment — AWS

Operator playbook for deploying the polling forwarder and (optionally)
the Cowork OTel collector on AWS.

## Prerequisites checklist

- [ ] **Cortex XSIAM tenant** with the *Settings → Data Sources →
      Add → Amazon S3 generic logs* onboarding screen accessible.
      Note the AWS account ID shown there — it's the same value for
      every data source.
- [ ] **Anthropic Compliance API enabled** for the org (any subset of
      feeds — `anthropic` only, `anthropic_chats` only, both, or neither).
      See [docs/vendors/anthropic.md](vendors/anthropic.md#enablement) for
      enablement steps.
- [ ] **OpenAI org with audit logging enabled** if you want
      `openai`/`openai_conversations`. See
      [docs/vendors/openai.md](vendors/openai.md#enablement).
- [ ] **API keys** for each feed you intend to deploy:
      - `anthropic` → `sk-ant-admin01-...` (Admin key, Activity Feed)
      - `anthropic_chats` → `sk-ant-api01-...` (Compliance Access Key,
        `read:compliance_user_data` scope, **Admin keys won't work**)
      - `openai` → `sk-admin-...`
      - `openai_conversations` → `sk-admin-...`
- [ ] **AWS account** with permission to create IAM roles, Lambda,
      DynamoDB, S3, SQS, Secrets Manager, EventBridge, CloudWatch.
- [ ] **Terraform ≥ 1.6**.
- [ ] **(Cowork OTel only)** VPC with ≥2 public subnets in different AZs,
      ACM certificate for the hostname you'll use, DNS control over that
      hostname.

## Deploy the polling forwarder

### 1. Configure the variables file

```hcl
# terraform/aws/example.tfvars  (gitignored)
region               = "us-east-1"
xsiam_aws_account_id = "<from XSIAM Add-Data-Source screen>"

vendors = {
  # Audit feeds (low volume, 5-min cadence is fine)
  anthropic            = { schedule_minutes = 5 }
  openai               = { schedule_minutes = 5 }

  # Content feeds (high volume — start with a 10-min initial lookback)
  anthropic_chats      = { schedule_minutes = 5, initial_lookback_minutes = 10 }
  openai_conversations = { schedule_minutes = 5, initial_lookback_minutes = 10 }
}

api_keys = {
  anthropic            = "sk-ant-admin01-..."
  anthropic_chats      = "sk-ant-api01-..."
  openai               = "sk-admin-..."
  openai_conversations = "sk-admin-..."
}
```

To deploy a subset, omit the unwanted keys from **both** maps. For
example, audit feeds only:

```hcl
vendors  = { anthropic = {}, openai = {} }
api_keys = { anthropic = "sk-ant-admin01-...", openai = "sk-admin-..." }
```

### 2. Apply

```bash
cd terraform/aws
terraform init
terraform plan -var-file=example.tfvars
terraform apply -var-file=example.tfvars
```

First apply takes ~3 minutes (Lambda package upload + IAM propagation).

### 3. Configure XSIAM data sources

For each feed, create one XSIAM *Amazon S3 generic logs* data source.
The fields come from these Terraform outputs:

| XSIAM field | Terraform output |
|---|---|
| Role ARN | `xsiam_role_arn` (shared across feeds) |
| External ID | `xsiam_external_id` (sensitive, shared) |
| SQS queue URL | `xsiam_sqs_urls[<feed>]` (per feed) |
| Bucket | `audit_bucket` (shared) |
| Log type | Custom; we recommend one log type per feed |

```bash
terraform output xsiam_sqs_urls
terraform output -raw xsiam_external_id
```

For dataset organization recommendations and parser hints, see
[docs/xsiam-integration.md](xsiam-integration.md).

### 4. Verify

Wait ~5–10 minutes for the first scheduled tick. CloudWatch logs:

```bash
aws logs tail "/aws/lambda/genai-audit-xsiam-forwarder-anthropic" --follow
```

Expected entries: `starting run first_run=True window=[...]`, then
`wrote N events to s3://...`, then `run complete`. If `forwarded=0` on
the first tick, that means the lookback window had no events — also
fine.

In XSIAM, run the verification XQL from
[docs/xsiam-integration.md](xsiam-integration.md#xql-recipes).

## Deploy the Cowork OTel collector (optional)

The Cowork collector reuses the parent stack's audit bucket but adds its
own Fargate cluster, ALB, SQS queue, and IAM role.

### 1. Pre-deploy

You need:
- The parent stack's `audit_bucket`, `xsiam_aws_account_id`, and
  `xsiam_external_id` outputs
- A VPC and ≥2 public subnets in different AZs
- An ACM certificate matching the hostname you'll use (e.g.
  `cowork-otel.soc.example.com`)

### 2. Apply

```bash
cd cowork-otel/terraform/aws
terraform init
terraform apply \
  -var "audit_bucket=$(cd ../../../terraform/aws && terraform output -raw audit_bucket)" \
  -var "xsiam_aws_account_id=123456789012" \
  -var "xsiam_external_id=$(cd ../../../terraform/aws && terraform output -raw xsiam_external_id)" \
  -var "vpc_id=vpc-xxx" \
  -var 'public_subnet_ids=["subnet-aaa","subnet-bbb"]' \
  -var "acm_certificate_arn=arn:aws:acm:..." \
  -var "hostname=cowork-otel.soc.example.com"
```

### 3. Add the bucket notification (one-time, in the parent stack)

The Cowork OTel stack creates an SQS queue but **does not** install the
S3 → SQS notification — that's owned by the parent bucket and managing
it from two stacks would conflict. After applying the Cowork stack, run
the AWS CLI command shown in `terraform output s3_notification_setup`,
or add the queue notification block to `terraform/aws/main.tf`'s
`aws_s3_bucket_notification.audit` resource.

### 4. Point the agents at the collector

- Bearer token: `terraform output -raw bearer_token`
- Endpoint: `https://<hostname>/v1/logs`

For Cowork: *Anthropic admin portal → Organization settings → Cowork →
OTLP endpoint*. Add header `Authorization: Bearer <token>`.

For Claude Code: see [cowork-otel/README.md](../cowork-otel/README.md)
for the managed-settings env var snippet.

### 5. Configure XSIAM

One additional *Amazon S3 generic logs* data source against the Cowork
SQS queue (`terraform output xsiam_sqs_url` from the Cowork stack).
Same role ARN + external ID as the parent stack — the Cowork stack adds
its own IAM role with access to the `cowork/` prefix.

## Outputs reference

| Output | Type | Used for |
|---|---|---|
| `lambda_function_names` | `map(string)` | CloudWatch logs lookup |
| `log_groups` | `map(string)` | CloudWatch alarm targets |
| `state_table` | `string` | DynamoDB lookup if you ever need to reset a feed |
| `xsiam_role_arn` | `string` | XSIAM data source onboarding |
| `xsiam_external_id` | `string` (sensitive) | XSIAM data source onboarding |
| `xsiam_sqs_urls` | `map(string)` | XSIAM data source onboarding (one per feed) |
| `audit_bucket` | `string` | XSIAM data source onboarding |

## Common errors

| Error | Cause | Fix |
|---|---|---|
| `vendors map keys must be one of: anthropic, anthropic_chats, openai, openai_conversations` | Typo in vendor key | Use exact names from the list |
| `Variables not allowed` on `terraform validate` | `${...}` in a description heredoc | Escape with `$$` |
| `NoRegionError: You must specify a region` from boto3 in tests | Module-level boto3 client construction outside Lambda | Already fixed (lazy-init); pull latest |
| Lambda errors with `AnthropicComplianceAPIError ... HTTP 403` | Compliance API not enabled, OR using Admin key for `anthropic_chats` | Enable Compliance API; for content feeds, use `sk-ant-api01-` Compliance Access Key |
| Lambda errors with `OpenAIAuditAPIError ... HTTP 403` | Audit logging not enabled in OpenAI org settings | Enable in *Org settings → Data controls → Data retention → Audit logging* |
| XSIAM dataset is empty after first tick | Either no events occurred in the lookback window, OR the SQS queue has no S3 notifications wired | Run an admin action in the source platform; check `aws_s3_bucket_notification` config |

For runbook-level operations (key rotation, backfill, alarms) see
[docs/operations.md](operations.md).
