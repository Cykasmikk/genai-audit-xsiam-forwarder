# Coverage

What this forwarder captures, what it doesn't, and what to do about
the gaps.

## At a glance

Five feeds across two vendors, plus an OTel collector for inference
telemetry. Within each feed there is **no event-type filter applied** —
we paginate to exhaustion and forward every event the vendor emits in
the time window.

| Feed | What you get | Source spec |
|---|---|---|
| `anthropic` | Admin/auth/system + resource activity (~200 event types) | [Anthropic Compliance API Rev J 2026-04-20](vendors/anthropic.md) |
| `anthropic_chats` | Full Claude.ai chat transcripts: prompts, responses, file references, artifacts | Same Rev J PDF, content endpoints |
| `openai` | Admin/auth/project activity (51 event types) | [OpenAI Audit Logs API](vendors/openai.md#audit-logs-api) |
| `openai_conversations` | Full ChatGPT Enterprise/Edu conversation transcripts | Skeleton — see [Coverage gaps](#coverage-gaps) |
| Cowork OTel | Cowork + Claude Code prompts, tool calls, file access, model + token + cost per request | Standard OTel logs/metrics |

## Full event-type inventories

### `anthropic` — Compliance API Activity Feed (Rev J)

Categories captured (one or more events per category, ~200 types total):

API • Accounts • Admin API Keys • Artifacts • Authentication • Billing
• Chat Snapshots • Chats (metadata) • Claude Code • Customizations
(commands/plugins/skills) • Files (metadata) • GitHub Enterprise •
Groups • Integrations (gdrive/github) • LTI • Marketplaces • MCP Servers
• Org Management • Org Discoverability • Org Settings • Platform Files •
Platform Org Mgmt • Platform Skills • Plugins • Projects (metadata) •
Pubsec • RBAC Roles • SCIM Provisioning • SSO & Directory Sync •
Service Keys • Session Shares • Signing Keys • User Settings.

Notable individual types for SOC use cases:

- `admin_api_key_created`, `admin_api_key_deleted`, `admin_api_key_updated`
- `sso_login_succeeded`, `sso_login_failed`, `magic_link_login_failed`
- `compliance_api_accessed` — every Compliance API request itself logs
  one of these. Useful for detecting anomalous forwarder activity.
- `org_compliance_api_settings_updated`,
  `org_ip_restriction_{created,deleted,updated}`,
  `org_sso_{toggled,connection_*}`
- `claude_file_uploaded`, `claude_file_deleted`,
  `claude_chat_created`, `claude_chat_deleted`
- `scim_user_created`, `group_member_added`,
  `rbac_role_assignment_granted`

### `anthropic_chats` — chat content endpoints

One AuditEvent per chat message, wrapped:

```json
{
  "chat": {
    "id": "claude_chat_abc",
    "name": "...",
    "organization_id": "org_x",
    "project_id": "claude_proj_xyz",
    "user": {"id": "user_y", "email_address": "alice@example.com"},
    "created_at": "...", "updated_at": "..."
  },
  "message": {
    "id": "claude_chat_msg_abc",
    "role": "user",
    "created_at": "...",
    "content": [{"type": "text", "text": "..."}],
    "files": [{"id": "claude_file_xyz", "filename": "...", "mime_type": "..."}],
    "artifacts": [{"id": "...", "version_id": "...", "title": "..."}]
  }
}
```

File binary content is **not** included by default. Set
`ANTHROPIC_FETCH_FILE_CONTENT=1` to inline file bytes as base64 on each
file reference (see [volume warning](#volume-warning)).

### `openai` — Audit Logs API (51 event types)

`api_key.{created,updated,deleted}` • `certificate.{created,updated,deleted,activated,deactivated}` • `checkpoint.permission.{created,deleted}` • `external_key.{registered,removed}` • `group.{created,updated,deleted}` • `invite.{sent,accepted,deleted}` • `ip_allowlist.{created,updated,deleted,config.activated,config.deactivated}` • `login.{succeeded,failed}` • `logout.{succeeded,failed}` • `organization.updated` • `project.{created,updated,archived,deleted}` • `rate_limit.{updated,deleted}` • `resource.deleted` • `tunnel.{created,updated,deleted}` • `role.{created,updated,deleted}` • `role.assignment.{created,deleted}` • `scim.{enabled,disabled}` • `service_account.{created,updated,deleted}` • `user.{added,updated,deleted}`

### `openai_conversations` — ChatGPT conversation transcripts

Schema not publicly documented. The adapter handles both plausible
response shapes (per-message and per-conversation with embedded
messages) and synthesizes a content-hash event id when the response
omits one. See [Coverage gaps](#coverage-gaps) below.

### Cowork OTel

Per [Anthropic's Cowork OTel doc](https://support.claude.com/en/articles/14477985-monitor-claude-cowork-activity-with-opentelemetry):

- Full text of prompts users submit
- Tool / MCP invocations: server name, tool name, parameters,
  success/failure, exec time
- File access paths (read, modified, MCP-mediated)
- Skills and plugins invoked
- Human approval decisions (approved / rejected / auto-permitted)
- Per-request: model, token counts, cost, duration, errors
- Shared `prompt.id` linking all events from one user input
- User email

For Claude Code on developer workstations, the same data plus standard
OTel env-var-driven telemetry (`CLAUDE_CODE_ENABLE_TELEMETRY=1`,
`OTEL_LOG_USER_PROMPTS=1`, `OTEL_LOG_TOOL_DETAILS=1`).

## Coverage map by SOC question

| SOC question | Feed | Notes |
|---|---|---|
| Who created an API key? | `anthropic` (`admin_api_key_created`, `platform_api_key_created`) + `openai` (`api_key.created`) | Cross-vendor query — see [docs/xsiam-integration.md](xsiam-integration.md#xql-recipes) |
| Who logged in / failed login? | `anthropic` (`sso_login_*`, `magic_link_login_*`) + `openai` (`login.{succeeded,failed}`) | |
| What did user X type into Claude.ai? | `anthropic_chats` filtered by `chat.user.email_address` | Compliance Access Key required |
| What did user X type into ChatGPT? | `openai_conversations` filtered by `actor_user_id` | Endpoint partial-spec |
| What tools did Claude Code run? | Cowork OTel | `OTEL_LOG_TOOL_DETAILS=1` required on agent |
| What files did Claude touch? | Cowork OTel | File-access events |
| Who has SSO access? | `anthropic` + `openai` (`org_sso_*`, `scim.*`, `sso_user.*`) | |
| Who can use the Compliance API? | `anthropic` (`admin_api_key_*`, `org_compliance_api_settings_updated`) | Self-audit on the forwarder |
| API rate-limit changes? | `anthropic` (`platform_workspace_rate_limit_updated`) + `openai` (`rate_limit.updated`) | |
| Who downloaded files via Compliance API? | `anthropic` (`platform_file_content_downloaded`) | |

## Coverage gaps

### 1. OpenAI Conversations endpoint not publicly documented

The adapter ships with educated defaults aligned with the sibling Audit
Logs API conventions (Authorization Bearer, `effective_at[gte]/[lte]`
Unix-seconds bracket filter, `after` cursor pagination). Every
guesswork point is marked `TODO(openai-conversations-spec)`.

**Three paths to close this gap:**

- **Best path:** ask your XSIAM TAM whether your tenant has Palo Alto
  Networks' native [OpenAI ChatGPT Enterprise Compliance integration](https://live.paloaltonetworks.com/t5/community-blogs/announcing-openai-chatgpt-enterprise-compliance-integration/ba-p/595958)
  available. If yes, prefer it over the skeleton adapter — Palo
  maintains the spec drift.

- **Self-serve path:** get the Compliance Logs Platform spec from your
  OpenAI Enterprise rep. Override the placeholder via env var:

  ```bash
  OPENAI_CONVERSATIONS_PATH=/v1/<actual_path>
  ```

  If query parameters or response shape differ, edit the constants at
  the top of `src/forwarder/vendors/openai_conversations.py`.

- **Diagnostic path:** deploy the skeleton against a non-prod org and
  read the 400/404 error responses. The body of a 400 typically lists
  unexpected query parameters; iterate from there.

### 2. Programmatic API call bodies (Claude API, OpenAI API)

Neither vendor retains the bodies of API calls made programmatically.
The Audit Logs / Compliance APIs only capture admin/auth events for
*platform* activity, not request payloads.

To audit programmatic API usage:

- **OpenAI:** use the `store=true` parameter on the [Responses API](https://platform.openai.com/docs/api-reference/responses) — OpenAI then retains the response and you can pull it via the Responses retrieval endpoints. Add a fifth adapter against those endpoints.
- **Anthropic:** no equivalent server-side option. Log application-side
  before sending, or run a logging proxy (e.g.
  [LiteLLM proxy](https://docs.litellm.ai/), Bricklayer) and forward
  proxy access logs to XSIAM.

### 3. Cloud-deployed model usage

- **AWS Bedrock Claude:** doesn't appear in Anthropic's Compliance API.
  Use [CloudTrail Bedrock data events](https://docs.aws.amazon.com/bedrock/latest/userguide/logging-using-cloudtrail.html). Wire as a separate XSIAM data source; not in this repo's scope.
- **Azure OpenAI:** doesn't appear in OpenAI's Audit Logs. Use
  [Azure Monitor / Activity Logs](https://learn.microsoft.com/en-us/azure/azure-monitor/essentials/activity-log). Separate XSIAM data source.

### 4. Personal/consumer usage outside an org

ChatGPT Free/Plus and Claude.ai personal accounts aren't part of an
enterprise audit scope and aren't covered. By design.

## Volume warning

The two content feeds (`anthropic_chats`, `openai_conversations`) and
the Cowork OTel collector all carry full prompt/response text. **Volume
is 10–1000× the audit feeds.** Before enabling them, review:

- **XSIAM ingestion costs** — pricing is volume-based.
- **PII / data-classification policy** — prompts can contain regulated
  data. Options:
  - Configure XSIAM-side redaction processors on the dataset
  - Scope content feeds to a separate dataset with stricter access
    control
  - Apply a [Cowork OTel filter](https://support.claude.com/en/articles/14477985-monitor-claude-cowork-activity-with-opentelemetry) to redact at collector time
- **First-run lookback** — the default
  `INITIAL_LOOKBACK_MINUTES=60` is sane for audit feeds but **set it to
  10 or lower for first deploy of content feeds in a busy org** to
  avoid an overnight backfill flooding XSIAM.

## Edge cases that could silently lose data — and our protections

| Scenario | Protection |
|---|---|
| Slow tick overlaps next tick | `reserved_concurrent_executions=1` (AWS) / `max_instance_count=1` (GCP) serializes same-feed; EventBridge / Cloud Scheduler retries the next tick |
| Egress (S3 / Pub-Sub / HTTP) fails mid-batch | Watermark NOT advanced; next tick replays the same window; XSIAM dedupes by event id |
| Vendor API returns 5xx / 429 | Exponential backoff up to 4 attempts before run aborts; next tick replays |
| Vendor API returns 401/403 | Run aborts loudly with actionable message — but **the audit feed is silent in XSIAM until you fix it**. CloudWatch / Cloud Logging alarm on Lambda errors is recommended. See [operations.md](operations.md#alarms). |
| Clock skew at window boundary | 5-minute overlap window + id-based dedupe |
| First-run cold start | Default lookback = 60 min only — older events are NOT backfilled. Set `INITIAL_LOOKBACK_MINUTES` higher to backfill. |
| Upstream platform incident | Outside our control. The OpenAI Feb 2026 audit-log incident (env-var dropped in a service split) lost upstream data; we'd see XSIAM go quiet but couldn't recover the lost events. |
