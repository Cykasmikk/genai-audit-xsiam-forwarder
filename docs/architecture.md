# Architecture

This document explains how the forwarder is organized, what guarantees
the design provides, and why each piece exists. Audience: engineers
reading or extending the codebase.

## Vendor-adapter pattern

Every supported feed is a Python class that implements a single
protocol:

```python
# src/forwarder/vendors/__init__.py
class AuditClient(Protocol):
    vendor: str
    def fetch_window(
        self, starting_at: datetime, ending_at: datetime
    ) -> Iterator[AuditEvent]: ...
```

`fetch_window` paginates the vendor's API for a `[starting_at, ending_at]`
window and yields a stream of `AuditEvent`s. The common shape is
deliberately tiny:

```python
@dataclass
class AuditEvent:
    id: str         # stable per-event id used for dedupe
    created_at: str # ISO 8601 / RFC 3339 UTC string
    vendor: str     # lowercase key — drives state PK, S3 prefix, attrs
    raw: dict       # vendor-native payload, untouched for XSIAM parsing
```

`raw` carries the original vendor schema verbatim so XSIAM operators
configure parsers against the upstream documentation, not against a
translation layer. Adapters that emit Unix timestamps (OpenAI's
`effective_at`) convert to ISO for the common shape but leave `raw`
unchanged.

### Adapters today

| File | Vendor key | API |
|---|---|---|
| `vendors/anthropic_compliance.py` | `anthropic` | `GET /v1/compliance/activities` |
| `vendors/anthropic_chat_content.py` | `anthropic_chats` | `GET /v1/compliance/apps/chats[/{id}/messages]` |
| `vendors/openai_audit.py` | `openai` | `GET /v1/organization/audit_logs` |
| `vendors/openai_conversations.py` | `openai_conversations` | TBD — see `vendors/openai_conversations.py` docstring |

Adding a new vendor is one new file in `vendors/`, one branch in the
`_make_client` dispatch in each handler, and one entry in the Terraform
`vendors` map validation.

## Idempotency model

Each feed's tick:

1. **Loads state**: `{watermark, recent_ids}` for the feed's vendor key.
   Watermark = ISO timestamp of the latest event we've forwarded.
   `recent_ids` = bounded set of event IDs near the watermark for
   overlap-window dedupe.

2. **Queries** `[watermark - OVERLAP_SECONDS, now]`. The 5-min overlap
   absorbs vendor-side clock skew and out-of-order delivery.

3. **Drops** events whose `id` is already in `recent_ids`.

4. **Forwards** the survivors to the configured egress sink.

5. **Persists** the advanced watermark + refreshed ID set **only after**
   the egress sink ACKs. Persisting before ACK would risk losing events
   on a crash; persisting after every batch (instead of once at the end)
   limits the replay window.

State is namespaced per vendor:

- DynamoDB primary key: `{vendor}_audit_state`
- Firestore document id: `{vendor}_state` in collection
  `genai_audit_forwarder`
- Anthropic adapter reads the legacy single-vendor PK / collection as a
  fallback so an in-place upgrade from the pre-multi-vendor commit
  preserves dedupe history.

`recent_ids` is bounded at `MAX_RECENT_IDS = 10_000` — well above any
realistic overlap-window cardinality for either vendor while keeping
state docs ~1 MB max (DynamoDB item-size limit is 400 KB, Firestore is
1 MB; we stay under both).

## Parallel execution

The architecture is designed for **all feeds running concurrently**:

- **Per-feed Lambda / Cloud Function.** Terraform `for_each` over the
  vendors map produces one function, one schedule, one queue/topic, and
  one secret per feed. They run in independent compute environments and
  invoke at the same wall-clock minute.

- **State is vendor-namespaced.** Two feeds cannot read or clobber each
  other's state row.

- **Egress is vendor-partitioned.** S3 keys carry a `{vendor}/` prefix
  with per-vendor SQS notifications. Pub/Sub uses one topic and one
  pull subscription per vendor. XSIAM operators wire up one data
  source per feed.

- **Same-feed overlap is serialized.** Each Lambda has
  `reserved_concurrent_executions = 1` and each Cloud Function has
  `max_instance_count = 1`. If one tick exceeds the schedule interval,
  the next invocation is queued (Lambda) or the Pub/Sub delivery is
  retried (Cloud Function) — a slow Anthropic poll never races the
  next Anthropic poll on the state row. **Cross-feed concurrency is
  unaffected**: OpenAI runs while the slow Anthropic tick is still
  in-flight.

The smoke suite proves these guarantees:

- `test_parallel_execution_no_contention` — all feeds fired from threads
  against a shared lock-protected state simulator + shared bucket.
  Asserts each feed's events forwarded exactly once and state stays
  vendor-namespaced.
- `test_parallel_repeated_runs_dedupe_correctly` — two same-feed
  invocations hammered in parallel (the case the concurrency caps
  prevent in production but worth proving safe in code). Even on a race
  the egress receives only the input id set — no fabrication, no payload
  mutation.

## Dataflow

### AWS — native pattern

```
   ┌─────────────┐  rate(5m)   ┌────────────────┐   PutObject
   │ EventBridge │ ──────────▶ │     Lambda     │ ─────────────┐
   │  (per feed) │             │   (per feed)   │              ▼
   └─────────────┘             └───────┬────────┘     ┌─────────────────┐
                                       │              │ Shared bucket   │
              ┌────────────────────────┘              │  anthropic/     │
              ▼                                       │  anthropic_chats│
       ┌──────────────────┐                           │  openai/        │
       │ Anthropic /      │                           │  openai_convs/  │
       │ OpenAI APIs      │                           │  cowork/        │
       └──────────────────┘                           └────────┬────────┘
                                                               │ ObjectCreated
                                                               │ (prefix-filtered)
                                                               ▼
                                                     ┌──────────────────┐
                                                     │ SQS per feed     │
                                                     │  (DLQ each)      │
                                                     └─────────┬────────┘
                                                               │ XSIAM polls via
                                                               │ assumed role +
                                                               │ external_id
                                                               ▼
                                                     ┌──────────────────┐
                                                     │   Cortex XSIAM   │
                                                     │   one DS per feed│
                                                     └──────────────────┘
```

Cowork OTel runs as a separate ECS Fargate service receiving OTLP push,
exporting via the same `awss3` pattern under the `cowork/` prefix.

### GCP — native pattern

```
   ┌─────────────┐  cron */5    ┌────────────────┐   publish     ┌────────────────┐
   │  Scheduler  │ ─────────▶   │ Cloud Function │ ──────────▶   │  audit topic   │
   │ (per feed)  │              │   (per feed)   │               │   (per feed)   │
   └─────────────┘              └────────┬───────┘               └────────┬───────┘
                                         │                                │
        ┌────────────────────────────────┘                                │
        ▼                                                                 ▼
  ┌──────────────────┐                                            ┌────────────────┐
  │ Anthropic /      │                                            │ pull sub       │
  │ OpenAI APIs      │                                            │  (per feed)    │
  └──────────────────┘                                            └────────┬───────┘
                                                                           │ XSIAM pulls via
                                                                           │ shared SA cred
                                                                           ▼
                                                                ┌──────────────────┐
                                                                │   Cortex XSIAM   │
                                                                │   one DS per feed│
                                                                └──────────────────┘
```

Cowork OTel runs as a separate Cloud Run service exporting via the
`googlecloudpubsub` exporter to its own topic.

## Egress sinks

The forwarder core is sink-agnostic; concrete sinks live in
`src/forwarder/egress/`.

| Sink | Cloud | Format | XSIAM ingestion |
|---|---|---|---|
| `S3Egress` | AWS | gzipped JSON-lines under `{vendor}/{prefix}/yyyy/mm/dd/hh/<ts>-<uuid>.jsonl.gz` | "Amazon S3 generic logs" with SQS notification |
| `PubSubEgress` | GCP | One Pub/Sub message per event, vendor + actor metadata as attributes | "GCP Pub/Sub" data source |
| `HttpEgress` | either | Direct POST to XSIAM HTTP Collector with `_vendor`/`_product`/`_time` envelope | HTTP Collector Custom App (fallback only) |

S3 and Pub/Sub are the documented native paths and the default. HTTP
fallback exists for cases where neither cloud-native path is available;
its auth header name and gzip support are not authoritatively documented
by Palo Alto, so verify against the operator's collector configuration
screen before production use.

## Why one Lambda / Function per feed

Alternatives considered:

| Design | Why we didn't pick it |
|---|---|
| Single Lambda iterates all feeds | Failure of one feed blocks others. Memory / timeout sized for the worst feed. State writes interleaved on shared row → race risk. |
| Single Lambda + per-feed thread pool | Adds complexity inside the Lambda for no operational benefit. Cold start cost paid once per tick instead of once per feed, but warm reuse covers this anyway. |
| One Lambda per feed (chosen) | Clean blast radius — disable a feed by removing it from the `vendors` map. Independent IAM scopes per feed. Independent CloudWatch / Cloud Logging streams. Concurrent execution between feeds is automatic. |

The marginal cost of a second Lambda is zero on the AWS free tier and
~$0.20/month at our default 5-min cadence at scale.

## Why XSIAM gets one data source per feed

Same reasoning. SOC operators want the ability to:
- Apply different parsers per feed (audit JSON vs. content JSON vs.
  OTLP-JSON).
- Apply different retention policies (audit metadata can stay 6 years,
  content might be 90 days under PII policy).
- Apply different alerting rules (chat content needs DLP regex; audit
  metadata needs admin-action correlation).

Mixing feeds in a single dataset works against all three.

## Failure semantics

| Failure | Behavior | Replay |
|---|---|---|
| Vendor API 429 / 5xx | Exponential backoff, up to 4 attempts, then run aborts | Next tick replays the same window |
| Vendor API 401/403/404 | Run aborts loudly with actionable error | Operator fixes; next tick replays |
| Vendor API 400 | Run aborts; structured error message surfaced | Operator fixes; next tick replays |
| Egress write fails | Watermark NOT advanced | Next tick replays; XSIAM dedupes by event id if the failed write actually landed |
| Lambda / Function crash mid-batch | Watermark NOT advanced past the unflushed batch | Next tick replays |
| Egress succeeds, state save fails | Possible duplicate forward on next tick | XSIAM-side dedupe |
| Concurrent same-feed runs (cap bypassed) | Both reads see same state; both shipped events covered by dedupe at the egress level | XSIAM-side dedupe |

The forwarder errs on the side of **at-least-once delivery** with
client-side and XSIAM-side dedupe; we never advance state past
unconfirmed-delivery work.
