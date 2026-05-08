# Operations runbook

Day-2 operational procedures for the on-call SRE. Pair with
[docs/architecture.md](architecture.md) for design context and
[docs/security.md](security.md) for access boundaries.

## Health signals

For each feed, a healthy steady state looks like:

| Cloud | Healthy heartbeat |
|---|---|
| AWS | `/aws/lambda/genai-audit-xsiam-forwarder-{feed}` log group emits one `run complete` line every `schedule_minutes` |
| GCP | Cloud Function `genai-audit-xsiam-forwarder-{feed}` invocations every `schedule_minutes`, each ending in `run complete` |
| XSIAM | Per-feed dataset receives at least one event in any 6-hour rolling window during business hours (real volume varies by source) |

The Cowork OTel collector emits its own logs via the OTel collector's
internal logger; healthy heartbeat is HTTP 200/202 responses to
`/v1/logs` requests in the ECS / Cloud Run access log.

## Alarms

We do **not** ship CloudWatch / Cloud Logging alarms in the Terraform
because alarm thresholds are SOC-policy-specific. Recommended:

### AWS

```hcl
# Per-feed: Lambda errors > 0 in 5 min → Pager
resource "aws_cloudwatch_metric_alarm" "lambda_errors" {
  for_each            = var.vendors
  alarm_name          = "${var.name_prefix}-${each.key}-errors"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "Errors"
  namespace           = "AWS/Lambda"
  period              = 300
  statistic           = "Sum"
  threshold           = 0
  dimensions = { FunctionName = aws_lambda_function.forwarder[each.key].function_name }
  alarm_actions = [var.pager_sns_topic_arn]
}

# Per-feed: no successful invocation in 30 min → ticket
resource "aws_cloudwatch_metric_alarm" "lambda_invocations_low" {
  for_each            = var.vendors
  alarm_name          = "${var.name_prefix}-${each.key}-stale"
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 6
  datapoints_to_alarm = 6
  metric_name         = "Invocations"
  namespace           = "AWS/Lambda"
  period              = 300
  statistic           = "Sum"
  threshold           = 1
  dimensions = { FunctionName = aws_lambda_function.forwarder[each.key].function_name }
  alarm_actions = [var.ticket_sns_topic_arn]
}
```

Add to `terraform/aws/main.tf` (or a sibling `alarms.tf`).

### GCP

```hcl
resource "google_monitoring_alert_policy" "function_errors" {
  for_each     = var.vendors
  display_name = "${var.name_prefix}-${each.key}-errors"
  combiner     = "OR"
  conditions {
    display_name = "Error rate > 0"
    condition_threshold {
      filter = <<EOT
        resource.type = "cloud_run_revision" AND
        resource.labels.service_name = "${var.name_prefix}-${each.key}" AND
        metric.type = "run.googleapis.com/request_count" AND
        metric.labels.response_code_class = "5xx"
      EOT
      duration   = "300s"
      comparison = "COMPARISON_GT"
      threshold_value = 0
    }
  }
  notification_channels = [var.pager_channel_id]
}
```

## Runbook entries

### "I need to rotate an API key"

1. Generate a new key out-of-band:
   - Anthropic Admin: *Console → Settings → Admin keys → Create new*
   - Anthropic Compliance Access: *Claude.ai → Org settings → Data and
     Privacy → Compliance access keys → Create*
   - OpenAI Admin: *Platform dashboard → Admin keys → Create new admin key*
2. Update the Terraform variable in `example.tfvars` for the affected
   feed.
3. `terraform apply` — Terraform creates a new Secrets Manager / Secret
   Manager *version* and the Lambda / Function picks it up at the next
   cold start (usually within 1–2 ticks). To force immediate cutover,
   manually invoke the Lambda or trigger the Scheduler job after apply.
4. Disable the old key in the vendor console once the new one is
   confirmed working (XSIAM dataset still receiving events).

### "I need to rotate the Cowork OTel bearer token"

```bash
cd cowork-otel/terraform/aws    # or gcp
terraform apply -replace=random_password.bearer_token
```

Then update the **Anthropic admin portal** Cowork OTLP header and the
Claude Code managed-settings rollout to use the new token. Until both
are updated, agents will get 401 from the collector.

### "I need to backfill a feed"

The forwarder caps first-run lookback at `INITIAL_LOOKBACK_MINUTES` to
prevent flooding XSIAM. To force a longer backfill on a feed that's
already been deployed:

1. Find the state record. AWS: `aws dynamodb get-item --table-name
   genai-audit-xsiam-forwarder-state --key '{"pk":{"S":"<feed>_audit_state"}}'`.
   GCP: `gcloud firestore documents describe genai_audit_forwarder/<feed>_state`.
2. Update its `watermark` to the older time you want to backfill from
   (RFC 3339 string).
3. Run the Lambda / Function manually (`aws lambda invoke ...` or
   `gcloud scheduler jobs run ...`).
4. The next run will query
   `[your_watermark - 5min, now]` and forward everything it finds.

**Anthropic retains 6 years of Activity Feed data**; OpenAI retention
is per their data retention policy. You can backfill arbitrarily far.

### "I need to reset a feed (forget all dedupe state)"

Delete the state record:

```bash
# AWS
aws dynamodb delete-item \
  --table-name genai-audit-xsiam-forwarder-state \
  --key '{"pk":{"S":"<feed>_audit_state"}}'

# GCP
gcloud firestore documents delete \
  genai_audit_forwarder/<feed>_state
```

Next tick re-pulls the last `INITIAL_LOOKBACK_MINUTES` and re-forwards
everything in that window. XSIAM will dedupe by event id if the
dataset is configured for it.

### "Lambda is throttling because runs take too long"

Default `reserved_concurrent_executions = 1` serializes same-feed
invocations. EventBridge will queue throttled invocations for retry up
to 24 hours, so no data is lost — but if the steady state has runs
taking >5 min, you have backlog accumulating.

Diagnose:
```bash
aws logs tail "/aws/lambda/genai-audit-xsiam-forwarder-<feed>" --since 1h \
  | grep "run complete" | awk '{print $NF}'
```

If `forwarded=` numbers are large, you're catching up from a backlog.
Options:
- **Wait it out.** Each 5-min tick processes the next chunk; eventually
  caught up.
- **Increase Lambda memory** (`memory_size = 1024`) — gives more CPU.
- **Decrease batch size** by lowering `PENDING_FLUSH_AT` in `core.py`.
- **Temporarily increase `schedule_minutes`** to 1 so backlog drains
  faster (this DOES cause concurrency throttling but EventBridge retries
  handle it).

### "Vendor returned 401/403 — what now?"

The forwarder raised `*APIError` with explicit guidance. Common cases:

| Vendor | Symptom | Fix |
|---|---|---|
| Anthropic Activity Feed | `read:compliance_activities scope` mentioned | Re-issue the Admin key after Compliance API was enabled |
| Anthropic chat content | "Compliance Access Key" mentioned | Replace `sk-ant-admin01-` with `sk-ant-api01-` for the `anthropic_chats` feed |
| OpenAI Audit Logs | "audit logging is enabled" | Enable in *Org settings → Data controls → Data retention* |
| Either | Key revoked / rotated by another admin | Re-issue and update the secret per "rotate API key" above |

The missed window is replayed automatically on the next tick once auth
is fixed — no manual backfill needed unless the outage exceeded the
vendor's retention.

### "XSIAM dataset went silent, but Lambda logs show success"

Step through the chain:

1. **S3 / Pub-Sub object created?** Check the bucket / topic for new
   objects since the last successful run timestamp.
2. **SQS queue receiving notifications?** AWS: check `ApproximateNumberOfMessagesSent`.
   GCP: check the subscription's unacked message count.
3. **XSIAM data source assumed-role / SA still authorized?** AWS:
   `aws sts assume-role --role-arn <xsiam_role_arn> --role-session-name diag --external-id <id>` — if this fails from a non-XSIAM account, that's expected; the issue is on XSIAM's side.
   GCP: check the subscription IAM bindings still include the XSIAM SA.
4. **XSIAM data source paused or in error state?** Check the data
   source onboarding screen.

### "I see duplicate events in XSIAM"

Likely causes (in order of likelihood):

1. **XSIAM dataset isn't configured to dedupe by event id.** Confirm
   the parser maps `id` (Anthropic) / `id` (OpenAI Audit) /
   `message.id` (anthropic_chats / openai_conversations) → the
   dataset's primary key.
2. **State document was deleted or rolled back, causing a re-forward.**
   See the reset/backfill procedures above.
3. **Two stacks deployed against the same vendor.** Check
   `terraform output` from both — the SQS queue / Pub/Sub topic should
   be unique per stack.
4. **Mid-batch crash before state save, then re-run.** Expected and
   safe IF dedupe is configured XSIAM-side. Otherwise duplicate-shipping
   is the dedupe model's tradeoff for never losing events.

## Cost dimensions

| Component | Per-feed cost driver | Order of magnitude |
|---|---|---|
| AWS Lambda | Invocations (12/hr × N feeds) + GB-seconds | < $1/mo per feed |
| AWS DynamoDB | Read+write per tick | < $1/mo total |
| AWS S3 | PUT requests + storage | $0.005 per 1000 PUTs; storage drives most |
| AWS SQS | Receives by XSIAM | Free tier covers it |
| AWS Secrets Manager | $0.40/secret/mo | $1.60/mo for 4 feeds |
| GCP Cloud Functions | Invocations + GB-seconds | < $1/mo per feed |
| GCP Firestore | Document reads/writes | Free tier |
| GCP Pub/Sub | Messages + retention | Volume-driven |
| GCP Secret Manager | $0.06/secret/mo | $0.24/mo for 4 feeds |
| Cowork OTel — AWS | Fargate task-hours + ALB-hours + S3 PUTs | ~$30–50/mo (2-task HA, 1 ALB) |
| Cowork OTel — GCP | Cloud Run + Pub/Sub | ~$10–20/mo (no ALB) |
| **XSIAM ingestion** | **Volume-based — dominates total cost** | Track in your XSIAM TAM dashboard |

## On-call playbook

When paged on `<feed>-errors`:

1. Look at the most recent Lambda / Function log line — the error
   message includes actionable guidance.
2. If it's an auth error, follow "Vendor returned 401/403".
3. If it's a 5xx from the vendor, wait one or two ticks for
   self-recovery.
4. If it's an egress error (`PutObject` / `publish` failing), check the
   IAM role / SA permissions and target resource (bucket / topic) state.
5. If unclear, set the Lambda alias's `traffic_config` to 0% temporarily
   to silence the alarm while you investigate, and re-run a single tick
   manually with `aws lambda invoke` / `gcloud scheduler jobs run`.

When paged on `<feed>-stale`:

1. Verify the EventBridge rule / Cloud Scheduler job is still enabled
   and matches the configured schedule.
2. Check IAM — did someone revoke the principal that EventBridge /
   Cloud Scheduler invokes with?
3. If neither, re-run the Lambda / Function manually and check whether
   it succeeds.

## Disaster recovery

The forwarder is stateless except for the per-feed watermark + recent
IDs in DynamoDB / Firestore.

- **Loss of state**: next run treats it as a first run with
  `INITIAL_LOOKBACK_MINUTES` lookback. Older events would need a manual
  backfill (see runbook entry).
- **Loss of audit bucket / Pub-Sub topic**: re-`terraform apply`
  recreates them, but events written between the loss and the
  next-tick failure are lost (the vendor still has them; backfill from
  the appropriate time).
- **Loss of XSIAM IAM role / SA**: `terraform apply` recreates with the
  same external ID / SA email. XSIAM data sources should reconnect on
  their next poll cycle.
