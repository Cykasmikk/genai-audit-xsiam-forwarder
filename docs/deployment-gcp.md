# Deployment — GCP

Operator playbook for deploying the polling forwarder and (optionally)
the Cowork OTel collector on GCP.

## Prerequisites checklist

- [ ] **Cortex XSIAM tenant** with the *Settings → Data Sources →
      Add → GCP Pub/Sub* onboarding screen accessible.
- [ ] **Anthropic Compliance API enabled** and/or **OpenAI org with
      audit logging enabled** for the feeds you want — see
      [docs/vendors/anthropic.md](vendors/anthropic.md#enablement) and
      [docs/vendors/openai.md](vendors/openai.md#enablement).
- [ ] **API keys** for each feed:
      - `anthropic` → `sk-ant-admin01-...`
      - `anthropic_chats` → `sk-ant-api01-...`
      - `openai` → `sk-admin-...`
      - `openai_conversations` → `sk-admin-...`
- [ ] **GCP project** with billing enabled and the Owner permission to
      create service accounts, Cloud Run services / Cloud Functions,
      Pub/Sub topics, Firestore, Secret Manager.
- [ ] **Terraform ≥ 1.6** and `gcloud` authenticated.
- [ ] **(Optional)** Firestore Native mode — Terraform creates the
      database, but if your project already has Datastore mode, you'll
      need a fresh project or migrate.

## Deploy the polling forwarder

### 1. Configure the variables file

```hcl
# terraform/gcp/example.tfvars  (gitignored)
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

Subset: omit feeds from both maps to skip them.

### 2. Apply

```bash
cd terraform/gcp
terraform init
terraform plan -var-file=example.tfvars
terraform apply -var-file=example.tfvars
```

First apply takes ~5 minutes (API enablement + Cloud Function build).

### 3. Generate the XSIAM service-account key

The Terraform creates a service account but **intentionally does not**
create a JSON key — keys in Terraform state are an audit smell. Create
one out-of-band, paste it into XSIAM, then delete the local file:

```bash
SA_EMAIL=$(terraform output -raw xsiam_service_account_email)
gcloud iam service-accounts keys create xsiam-credentials.json \
  --iam-account="$SA_EMAIL"
```

### 4. Configure XSIAM data sources

For each feed, create one XSIAM *GCP Pub/Sub* data source:

| XSIAM field | Source |
|---|---|
| Subscription name | `terraform output xsiam_audit_subscriptions[<feed>]` |
| Service account credentials | contents of `xsiam-credentials.json` |
| Log type | Custom; one per feed (see [xsiam-integration.md](xsiam-integration.md)) |

```bash
terraform output xsiam_audit_subscriptions
```

After all feeds are wired, **delete the local key file** —
`rm xsiam-credentials.json`.

### 5. Verify

Cloud Function logs:

```bash
gcloud functions logs read genai-audit-xsiam-forwarder-anthropic \
  --gen2 --region=us-central1 --limit=20
```

Expected: `starting run first_run=True`, then `published N events`,
then `run complete`. If `forwarded=0`, the lookback window had no
events.

In XSIAM, run the verification XQL from
[docs/xsiam-integration.md](xsiam-integration.md#xql-recipes).

## Deploy the Cowork OTel collector (optional)

Reuses the parent stack's XSIAM service account; adds its own Cloud Run
service, Pub/Sub topic, subscription, and bearer-token secret.

### 1. Pre-deploy

You need the parent stack's `xsiam_service_account_email` output. Cloud
Run is HTTPS-native — no certificate management needed.

### 2. Apply

```bash
cd cowork-otel/terraform/gcp
terraform init
terraform apply \
  -var "project_id=my-soc-project" \
  -var "region=us-central1" \
  -var "xsiam_service_account_email=$(cd ../../../terraform/gcp && terraform output -raw xsiam_service_account_email)"
```

### 3. Point the agents at the collector

- Bearer token: `terraform output -raw bearer_token`
- Endpoint: `terraform output -raw collector_endpoint` (Cloud Run URL),
  append `/v1/logs` for OTLP HTTP

Configure Cowork (admin portal) and Claude Code (managed settings) the
same as the AWS path — see [cowork-otel/README.md](../cowork-otel/README.md).

### 4. Configure XSIAM

One additional *GCP Pub/Sub* data source against the Cowork
subscription:

```bash
terraform output cowork_subscription
```

Use the **same** `xsiam-credentials.json` you generated for the parent
stack — the parent stack's SA was granted `pubsub.subscriber` on the
Cowork subscription too.

## Outputs reference

### Parent stack

| Output | Type | Used for |
|---|---|---|
| `function_names` | `map(string)` | Cloud Function logs lookup |
| `scheduler_jobs` | `map(string)` | Cloud Scheduler manual trigger |
| `xsiam_audit_topics` | `map(string)` | (Diagnostics) raw topic ids |
| `xsiam_audit_subscriptions` | `map(string)` | XSIAM data source onboarding (one per feed) |
| `xsiam_service_account_email` | `string` | Generate credentials JSON for XSIAM |

### Cowork OTel stack

| Output | Type | Used for |
|---|---|---|
| `collector_endpoint` | `string` | Cowork / Claude Code OTLP endpoint URL |
| `bearer_token` | `string` (sensitive) | Authorization header for agents |
| `cowork_topic` | `string` | (Diagnostics) topic id |
| `cowork_subscription` | `string` | XSIAM data source onboarding |

## Common errors

| Error | Cause | Fix |
|---|---|---|
| `oauth2: "invalid_grant" "reauth related error (invalid_rapt)"` mid-apply | Workspace policy requires fresh reauth proof token; long Terraform applies hit it | Run `gcloud auth login <account> --update-adc --force` to get a fresh token, then re-run `terraform apply` (idempotent) |
| `Build failed: missing permission on the build service account` | Workspace org policy stripped Cloud Build SA defaults | The Terraform now explicitly grants `roles/cloudbuild.builds.builder` and `roles/logging.logWriter` to the compute SA — re-apply if you see this on an old version |
| Cloud Function 401 on first trigger: `IAM principal lacks {run.routes.invoke} permission` | Eventarc trigger principal lacks `run.invoker` on the Cloud Run service backing the function | The Terraform now sets the trigger SA to the per-vendor function SA and grants `roles/run.invoker` on the Cloud Run service to it. Re-apply if drift |
| `Error 403: Permission denied` during `terraform apply` | User account missing Owner / required roles on the project | Grant Owner or scoped equivalent |
| `Error: googleapi: Error 409: ALREADY_EXISTS` for Firestore database | Project already has Datastore-mode database | Use a fresh project or migrate (Firestore can't switch modes) |
| Cloud Function deploy fails with `requirements.txt not found` | Source archive excludes `requirements.txt` | Check `terraform/gcp/main.tf`'s `archive_file.fn_src.excludes` — `requirements.txt` should NOT be in the excludes list |
| `OpenAIAuditAPIError ... HTTP 403` at runtime | Audit logging not enabled in OpenAI org | Enable in *Org settings → Data controls → Data retention → Audit logging* |
| `AnthropicComplianceAPIError ... HTTP 404` at runtime | Org doesn't have Compliance API enabled | Anthropic returns 404 (not 403) for orgs without Compliance API enablement. Enable per [vendors/anthropic.md](vendors/anthropic.md#enablement). |
| XSIAM dataset empty | Subscription has no published messages, OR XSIAM SA can't pull | Verify locally with `gcloud pubsub subscriptions pull <name>` — if you can pull, the SA can too |

For runbook-level operations see [docs/operations.md](operations.md).
