# XSIAM integration

XSIAM-side configuration for ingesting the five feeds, recommended
dataset organization, and an XQL recipe library for SOC investigations.

## Data source onboarding by ingestion path

### AWS — *Amazon S3 generic logs*

Used for: all four polling feeds (`anthropic`, `anthropic_chats`,
`openai`, `openai_conversations`) and the Cowork OTel collector — five
data sources total when fully deployed.

For each feed, *Settings → Data Sources → Add Data Source → Amazon S3
generic logs*:

| Field | Source |
|---|---|
| Role ARN | Terraform output `xsiam_role_arn` (shared across feeds) |
| External ID | Terraform output `xsiam_external_id` (sensitive, shared) |
| SQS URL | `xsiam_sqs_urls[<feed>]` per feed (or `xsiam_sqs_url` from cowork-otel stack) |
| S3 bucket | `audit_bucket` (shared) |
| Log type | Custom — recommend: `claude_compliance_audit`, `claude_compliance_chats`, `openai_audit`, `openai_conversations`, `claude_cowork_otel` |
| Vendor | `Anthropic` / `OpenAI` |
| Product | match the log type |
| Compression | gzip |
| Format | JSON / NDJSON |

After save, XSIAM begins polling SQS for ObjectCreated notifications.
First events appear in the dataset within 5–10 minutes.

### GCP — *GCP Pub/Sub*

For each feed, *Settings → Data Sources → Add Data Source → GCP
Pub/Sub*:

| Field | Source |
|---|---|
| Subscription name | `terraform output xsiam_audit_subscriptions[<feed>]` (or `cowork_subscription` from cowork-otel) |
| Service account credentials | Contents of the `xsiam-credentials.json` you generated out-of-band |
| Project ID | Your GCP project id |
| Log type / Vendor / Product | Same recommendations as AWS path above |

The same SA credential is used for every feed including Cowork OTel —
the parent Terraform stack grants `roles/pubsub.subscriber` on every
feed's subscription.

### XSIAM HTTP Collector (fallback only)

Not the recommended path. If you must use it, see the egress sink
documentation in [docs/architecture.md](architecture.md#egress-sinks)
and the comments at the top of `src/forwarder/egress/http.py`.

## Recommended dataset organization

```
<your_xsiam_tenant>
├── claude_compliance_audit          ← anthropic feed (admin/auth events)
├── claude_compliance_chats          ← anthropic_chats feed (transcripts) [Restricted]
├── openai_audit                     ← openai feed (admin/auth events)
├── openai_conversations             ← openai_conversations feed (transcripts) [Restricted]
└── claude_cowork_otel               ← Cowork + Claude Code OTel (transcripts) [Restricted]
```

Apply different access control + retention to the `[Restricted]`
datasets; see [docs/security.md](security.md#data-classification).

## Parser hints

The forwarder writes the **raw vendor-native payload** as the event
body. XSIAM parsing rules should map upstream field names to the
dataset's standard fields. Suggested mappings:

### `claude_compliance_audit` (Anthropic Activity Feed)

| Vendor field | XSIAM standard / suggested |
|---|---|
| `id` | event id |
| `created_at` | `_time` |
| `type` | event type / category |
| `actor.type` | discriminator |
| `actor.email_address` | `user.email` |
| `actor.user_id` | `user.id` |
| `actor.ip_address` | `source.ip` |
| `actor.user_agent` | `user_agent.original` |
| `organization_id` | `organization.id` |

### `claude_compliance_chats` (Anthropic chat content)

The forwarder wraps each event as `{chat: meta, message: content}`:

| Path | XSIAM standard / suggested |
|---|---|
| `message.created_at` | `_time` |
| `message.id` | event id |
| `message.role` | `role` (user / assistant) |
| `message.content[].text` | `content.text` (concatenate array) |
| `chat.id` | `conversation.id` |
| `chat.user.id` | `user.id` |
| `chat.user.email_address` | `user.email` |
| `chat.organization_id` | `organization.id` |
| `chat.project_id` | `project.id` |

### `openai_audit` (OpenAI Audit Logs)

| Vendor field | XSIAM standard / suggested |
|---|---|
| `id` | event id |
| `effective_at` | `_time` (Unix→datetime conversion in the parser) |
| `type` | event type |
| `actor.session.user.email` / `actor.api_key.user.email` | `user.email` |
| `actor.session.user.id` / `actor.api_key.user.id` | `user.id` |
| `actor.session.ip_address` | `source.ip` |
| `project.id` | `project.id` |

### `openai_conversations` (skeleton)

Adapter wraps as `{conversation: meta, message: content}` if the spec
returns conversation-level records. Parser shape will depend on the
real spec — start with the `claude_compliance_chats` mapping as a
template and adjust once you have the OpenAI Compliance Logs Platform
spec.

### `claude_cowork_otel` (OTel logs)

OTel format. The OTel Collector exporters serialize to JSON-lines
(awss3) or OTLP-JSON (googlecloudpubsub). Each record has:

| Path | XSIAM standard / suggested |
|---|---|
| `Body` (text or structured) | message content |
| `Timestamp` (Unix nanos) | `_time` |
| `Attributes["event.name"]` | event type (e.g. `prompt`, `tool_call`) |
| `Attributes["user.email"]` | `user.email` |
| `Attributes["prompt.id"]` | correlation id linking events from one prompt |
| `Resource["_vendor"]` | `anthropic` |
| `Resource["_product"]` | `claude_cowork` |

## XQL recipes

### Verification (run after first deploy)

```xql
dataset = claude_compliance_audit
| sort desc _time
| limit 10
```

```xql
dataset = openai_audit
| sort desc _time
| limit 10
```

### Self-audit — is the forwarder still running?

```xql
// Anthropic — every Compliance API request shows up here
dataset = claude_compliance_audit
| filter type = "compliance_api_accessed"
| comp count() by bin(_time, 5m)
```

```xql
// OpenAI — alert on no successful login events for >1h business hours
dataset = openai_audit
| filter type = "login.succeeded"
| comp count() by bin(_time, 1h)
| filter count = 0
```

### Identity & access

```xql
// Cross-vendor: any API key created in last 24h
dataset in (claude_compliance_audit, openai_audit)
| filter type in ("admin_api_key_created", "platform_api_key_created", "api_key.created")
| fields _time, _vendor, type, actor, api_key_id
| sort desc _time
```

```xql
// Failed logins by user, last 6h, sliding 5m bins
dataset in (claude_compliance_audit, openai_audit)
| filter type in ("sso_login_failed", "magic_link_login_failed", "login.failed")
| comp count() by bin(_time, 5m), actor.session.user.email, actor.email_address
| filter count > 5
```

```xql
// New SSO config / IP allowlist changes
dataset in (claude_compliance_audit, openai_audit)
| filter type matches regex "(sso_|ip_allowlist)"
| sort desc _time
```

### DLP on prompt content

```xql
// Claude.ai prompts mentioning a CCN-shaped number
dataset = claude_compliance_chats
| filter message.role = "user"
| filter message.content matches regex "\b\d{4}[ -]\d{4}[ -]\d{4}[ -]\d{4}\b"
| fields _time, chat.user.email_address, chat.organization_id, message.content
```

```xql
// Cowork prompts mentioning a secret pattern
dataset = claude_cowork_otel
| filter Body contains "AKIA" or Body contains "ghp_"
| fields _time, Attributes["user.email"], Body
```

```xql
// ChatGPT conversations involving a specific user (skeleton — adapt
// fields once the conversations spec is locked)
dataset = openai_conversations
| filter conversation.user_id = "user-abc"
| fields _time, message.role, message.content, message.model
| sort desc _time
```

### Tool calls and resource access

```xql
// Cowork — what tools did Claude run for user X?
dataset = claude_cowork_otel
| filter Attributes["event.name"] = "tool_call"
| filter Attributes["user.email"] = "alice@example.com"
| fields _time, Attributes["tool.name"], Attributes["tool.parameters"], Attributes["tool.outcome"]
```

```xql
// Cowork — files Claude touched in last 24h, by user
dataset = claude_cowork_otel
| filter Attributes["event.name"] = "file_access"
| comp count() by Attributes["user.email"], Attributes["file.path"]
```

### Compliance / audit posture

```xql
// All Compliance API setting changes (who turned on/off, when)
dataset = claude_compliance_audit
| filter type in ("org_compliance_api_settings_updated",
                  "audit_log_export_started",
                  "audit_log_export_accessed")
| sort desc _time
```

```xql
// OpenAI — admin actions in last 7 days
dataset = openai_audit
| filter type matches regex "(api_key|service_account|user|role|invite)\."
| comp count() by type, bin(_time, 1d)
```

### Anomaly detection

```xql
// Sudden spike in chat volume for a single user — possible compromised account
dataset = claude_compliance_chats
| filter message.role = "user"
| comp count() by bin(_time, 1h), chat.user.id
| filter count > 100
| sort desc count
```

```xql
// API keys used from new IPs (compare current IP set to last 30 days)
dataset = claude_compliance_audit
| filter type = "compliance_api_accessed"
| comp dc(actor.ip_address) by actor.api_key_id
```

## Cross-feed correlation

The polling feeds and the OTel feed share user identifiers. Per
Anthropic's Cowork doc, `prompt.id` is also a shared correlator across
all events from one prompt. Examples:

```xql
// Tie Cowork prompt.id back to Activity Feed user
dataset = claude_cowork_otel
| filter Attributes["prompt.id"] = "prompt_abc123"
| fields _time, Attributes["user.email"], Body
| join (
    dataset = claude_compliance_audit
    | filter type matches regex "claude_chat_"
    | fields _time, claude_chat_id, actor.user_id
  ) on user.email = actor.user_id
```

```xql
// Find the audit event that immediately followed a high-cost Cowork
// call (e.g. an admin action triggered by an investigation)
dataset = claude_cowork_otel
| filter Attributes["cost.usd"] > 1.0
| fields _time, Attributes["user.email"]
| join (
    dataset = claude_compliance_audit
    | filter type matches regex "(api_key|workspace|skill)_"
  ) on actor.user_id = user.email
  within 5m
```

## Onboarding checklist

For each feed when adding it to XSIAM:

- [ ] Add data source (S3 generic logs / Pub/Sub) per the table above
- [ ] Verify first events arrive within 10 minutes
- [ ] Configure parser to map vendor field names to standard fields
- [ ] (Restricted feeds) Apply access control limiting to authorized
      incident responders
- [ ] (Restricted feeds) Apply retention policy per data classification
- [ ] Add a self-audit alert (failed-login spike, forwarder
      `compliance_api_accessed` heartbeat absent)
- [ ] Document the feed in your SOC runbook (what queries answer what
      questions for this feed)
