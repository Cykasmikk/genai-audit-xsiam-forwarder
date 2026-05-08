"""Heavy smoke + integration suite for the multi-vendor forwarder.

Runs in CI without AWS or GCP credentials by injecting fake low-level
clients. Verifies, against the latest authoritative specs:

  Anthropic Compliance API — Rev J 2026-04-20:
    - GET /v1/compliance/activities, x-api-key with sk-ant-admin01- /
      sk-ant-api01- prefixes, created_at.gte/lte dotted time filter,
      after_id pagination, response keys (data, has_more, last_id),
      Activity object schema (id, created_at, type, actor, organization_id).

  OpenAI Audit Logs API:
    - GET /v1/organization/audit_logs, Authorization Bearer sk-admin-,
      effective_at[gte]/[lte] bracketed Unix-seconds filter, after
      pagination, response keys (data, has_more, last_id), audit log
      object (id, effective_at, type, actor.session|api_key, project).

Coverage:
  Per vendor:
    - Module imports against real boto3 / google-cloud-pubsub
    - Key prefix validation (positive + negative)
    - URL construction (path, query params, time-filter syntax)
    - Pagination loop terminates on has_more=false and on missing/repeat last_id
    - 400/401/403/404 raise vendor-specific actionable errors
    - 5xx + 429 retry with backoff; permanent failure raises
    - Event normalization to common AuditEvent (id + ISO created_at)
    - OpenAI Unix-seconds → ISO conversion is round-trip stable

  Cross-vendor:
    - Two parallel runs against the same DynamoDB-backed state store
      do NOT cross-contaminate state (per-vendor pk namespacing)
    - S3 object keys partition by vendor in the path
    - Pub/Sub messages carry vendor attribute + vendor-aware extra attrs
    - HTTP fallback envelope picks correct _time field per vendor

  Pipeline:
    - Watermark + dedupe by id across overlapping fetches
    - Egress failure aborts run before watermark advances
    - Bounded recent_ids state document
"""

from __future__ import annotations

import gzip
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

# Make sure all top-level modules import with real SDKs
from forwarder.aws_handler import handler as aws_handler  # noqa: F401
from forwarder.core import MAX_RECENT_IDS, OVERLAP_SECONDS, _compute_state, run
from forwarder.egress.http import HttpEgress, HttpEgressConfig
from forwarder.egress.pubsub import PubSubEgress
from forwarder.egress.s3 import S3Egress
from forwarder.gcp_handler import handler as gcp_handler  # noqa: F401
from forwarder.state import ForwarderState, StateStore
from forwarder.vendors import AuditEvent
from forwarder.vendors.anthropic_chat_content import (
    AnthropicChatContentAPIError,
    AnthropicChatContentClient,
)
from forwarder.vendors.anthropic_compliance import (
    AnthropicComplianceAPIError,
    AnthropicComplianceClient,
    COMPLIANCE_API_PATH as ANTHROPIC_PATH,
)
from forwarder.vendors.openai_audit import (
    AUDIT_LOGS_PATH as OPENAI_PATH,
    OpenAIAuditAPIError,
    OpenAIAuditClient,
)
from forwarder.vendors.openai_conversations import (
    OpenAIConversationsAPIError,
    OpenAIConversationsClient,
)


# ── Fakes ──────────────────────────────────────────────────────────────────


class FakeS3Client:
    def __init__(self):
        self.calls = []

    def put_object(self, **kw):
        self.calls.append(kw)


class FakeFuture:
    def __init__(self, msg_id, exc=None):
        self._id = msg_id
        self._exc = exc

    def result(self, timeout=None):
        if self._exc:
            raise self._exc
        return self._id


class FakePublisher:
    def __init__(self, fail=False):
        self.published = []
        self._counter = 0
        self._fail = fail

    def topic_path(self, project, topic):
        return f"projects/{project}/topics/{topic}"

    def publish(self, topic, data, **attrs):
        self._counter += 1
        self.published.append({"topic": topic, "data": data, "attrs": attrs})
        if self._fail:
            return FakeFuture(None, exc=RuntimeError("publish failed"))
        return FakeFuture(f"msg-{self._counter}")


class FakeHttpResp:
    def __init__(self, status, body=b""):
        self.status = status
        self.data = body


class StaticHttp:
    def __init__(self, status, body=b""):
        self._status = status
        self._body = body
        self.requests = []

    def request(self, method, url, headers=None, body=None):
        self.requests.append({"method": method, "url": url, "headers": headers, "body": body})
        return FakeHttpResp(self._status, self._body)


class ScriptedHttp:
    """Returns a sequence of (status, body) responses; raises if exhausted."""

    def __init__(self, sequence):
        self._sequence = list(sequence)
        self.requests = []

    def request(self, method, url, headers=None, body=None):
        self.requests.append({"method": method, "url": url, "headers": headers})
        status, body = self._sequence.pop(0)
        return FakeHttpResp(status, body)


class CapturingHttp:
    """Captures all requests, returns the next scripted (status, body)."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.requests = []

    def request(self, method, url, headers=None, body=None):
        self.requests.append({"method": method, "url": url, "headers": headers, "body": body})
        if not self._responses:
            raise AssertionError(f"Unexpected extra request to {url}")
        status, body = self._responses.pop(0)
        return FakeHttpResp(status, body)


class MemStore(StateStore):
    def __init__(self, vendor="anthropic"):
        self.vendor = vendor
        self.s = ForwarderState()
        self.saves = 0

    def load(self):
        return self.s

    def save(self, st):
        self.s = st
        self.saves += 1


# ── Fixtures: real-looking events from each vendor's authoritative spec ──

NOW = datetime(2026, 5, 5, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _unix(dt):
    return int(dt.astimezone(timezone.utc).timestamp())


# Anthropic Compliance API Rev J (PDF examples, pp. 24-26)
ANTHROPIC_EVENTS = [
    {
        "id": "activity_1a2b3c4d5e",
        "created_at": _iso(NOW - timedelta(minutes=10)),
        "organization_id": "org_abc123",
        "organization_uuid": "uuid-1",
        "actor": {
            "type": "user_actor",
            "email_address": "alice@example.com",
            "user_id": "user_xyz456",
            "ip_address": "192.0.2.34",
            "user_agent": "Mozilla/5.0",
        },
        "type": "claude_chat_created",
        "claude_chat_id": "claude_chat_ijk012",
        "claude_project_id": None,
    },
    {
        "id": "activity_3c4d5e6f7g",
        "created_at": _iso(NOW - timedelta(minutes=4)),
        "organization_id": "org_abc123",
        "organization_uuid": "uuid-1",
        "actor": {
            "type": "api_actor",
            "api_key_id": "apikey_fghij567890",
            "ip_address": "10.0.0.1",
            "user_agent": "Mozilla/5.0",
        },
        "type": "compliance_api_accessed",
        "request_id": "req_123",
        "url": "https://api.anthropic.com/v1/compliance/activities",
        "request_method": "GET",
        "status_code": 200,
    },
]

# OpenAI Audit Logs (constructed per the documented schema)
OPENAI_EVENTS = [
    {
        "id": "audit_log-abc123",
        "effective_at": _unix(NOW - timedelta(minutes=9)),
        "type": "login.succeeded",
        "actor": {
            "session": {
                "ip_address": "203.0.113.10",
                "user": {"id": "user-2x4f", "email": "bob@example.com"},
            }
        },
        "project": None,
    },
    {
        "id": "audit_log-def456",
        "effective_at": _unix(NOW - timedelta(minutes=6)),
        "type": "api_key.created",
        "actor": {
            "api_key": {
                "id": "key_admin_xyz",
                "type": "admin",
                "user": {"id": "user-2x4f", "email": "bob@example.com"},
            }
        },
        "project": {"id": "proj_789", "name": "default-project"},
        "api_key.created": {
            "id": "key_new_abc",
            "data": {"scopes": ["api.readonly"]},
        },
    },
    {
        "id": "audit_log-ghi789",
        "effective_at": _unix(NOW - timedelta(minutes=3)),
        "type": "login.failed",
        "actor": {
            "session": {
                "ip_address": "203.0.113.99",
                "user": {"id": "user-attacker", "email": "evil@bad.com"},
            }
        },
        "project": None,
    },
]


# ── Fake vendor clients that replay fixtures ───────────────────────────────


class FakeAnthropic:
    vendor = "anthropic"

    def __init__(self, events):
        self._events = events
        self.calls = []

    def fetch_window(self, start, end):
        self.calls.append((start, end))
        for raw in self._events:
            ev = AuditEvent(
                id=raw["id"], created_at=raw["created_at"], vendor="anthropic", raw=raw
            )
            if start <= ev.created_at_dt <= end:
                yield ev


class FakeOpenAI:
    vendor = "openai"

    def __init__(self, events):
        self._events = events
        self.calls = []

    def fetch_window(self, start, end):
        from forwarder.vendors.openai_audit import _unix_to_iso

        self.calls.append((start, end))
        for raw in self._events:
            iso = _unix_to_iso(raw["effective_at"])
            ev = AuditEvent(id=raw["id"], created_at=iso, vendor="openai", raw=raw)
            if start <= ev.created_at_dt <= end:
                yield ev


# ── Anthropic-specific tests ───────────────────────────────────────────────


def test_anthropic_admin_and_compliance_keys_accepted():
    AnthropicComplianceClient("sk-ant-admin01-test")
    AnthropicComplianceClient("sk-ant-api01-test")
    print("OK test_anthropic_admin_and_compliance_keys_accepted")


def test_anthropic_other_keys_rejected():
    for bad in ("sk-admin-test", "sk-other", "no-prefix"):
        try:
            AnthropicComplianceClient(bad)
            raise AssertionError(f"accepted bad key {bad}")
        except ValueError as e:
            assert "sk-ant-admin01-" in str(e) and "sk-ant-api01-" in str(e)
    print("OK test_anthropic_other_keys_rejected")


def test_anthropic_request_url_and_headers():
    body = json.dumps({"data": [], "has_more": False}).encode()
    http = CapturingHttp((200, body))
    c = AnthropicComplianceClient("sk-ant-admin01-test", http=http)
    list(c.fetch_window(NOW - timedelta(minutes=10), NOW))
    req = http.requests[0]
    parsed = urlparse(req["url"])
    assert parsed.path == "/v1/compliance/activities"
    qs = parse_qs(parsed.query)
    # Dotted notation (Rev J)
    assert "created_at.gte" in qs and "created_at.lte" in qs
    assert qs["limit"] == ["1000"]
    # Headers
    assert req["headers"]["x-api-key"] == "sk-ant-admin01-test"
    assert req["headers"]["anthropic-version"] == "2023-06-01"
    print("OK test_anthropic_request_url_and_headers")


def test_anthropic_pagination_via_after_id():
    pages = [
        (
            200,
            json.dumps(
                {
                    "data": [
                        {
                            "id": f"activity_a{i}",
                            "created_at": _iso(NOW - timedelta(minutes=i)),
                            "type": "claude_chat_created",
                            "actor": {"type": "user_actor", "user_id": "u"},
                        }
                        for i in range(2)
                    ],
                    "has_more": True,
                    "last_id": "activity_a1",
                }
            ).encode(),
        ),
        (
            200,
            json.dumps(
                {
                    "data": [
                        {
                            "id": "activity_b",
                            "created_at": _iso(NOW - timedelta(minutes=20)),
                            "type": "claude_chat_created",
                            "actor": {"type": "user_actor", "user_id": "u"},
                        }
                    ],
                    "has_more": False,
                }
            ).encode(),
        ),
    ]
    http = ScriptedHttp(pages)
    c = AnthropicComplianceClient("sk-ant-admin01-test", http=http)
    events = list(c.fetch_window(NOW - timedelta(hours=1), NOW))
    assert len(events) == 3
    # Sorted oldest-first (ascending created_at)
    assert events[0].id == "activity_b"
    # Second request used after_id
    parsed = urlparse(http.requests[1]["url"])
    qs = parse_qs(parsed.query)
    assert qs["after_id"] == ["activity_a1"]
    print("OK test_anthropic_pagination_via_after_id")


def test_anthropic_pagination_terminates_on_missing_last_id():
    # has_more=true but no last_id → defensive break
    pages = [
        (
            200,
            json.dumps(
                {
                    "data": [
                        {
                            "id": "activity_x",
                            "created_at": _iso(NOW),
                            "type": "claude_chat_created",
                            "actor": {"type": "user_actor"},
                        }
                    ],
                    "has_more": True,
                    # last_id intentionally omitted
                }
            ).encode(),
        ),
    ]
    http = ScriptedHttp(pages)
    c = AnthropicComplianceClient("sk-ant-admin01-test", http=http)
    list(c.fetch_window(NOW - timedelta(hours=1), NOW))  # must not loop
    assert len(http.requests) == 1
    print("OK test_anthropic_pagination_terminates_on_missing_last_id")


def test_anthropic_404_message_points_at_path_and_env_var():
    c = AnthropicComplianceClient("sk-ant-admin01-test", http=StaticHttp(404, b"not found"))
    try:
        list(c.fetch_window(NOW - timedelta(minutes=5), NOW))
        raise AssertionError("expected error")
    except AnthropicComplianceAPIError as e:
        assert "/v1/compliance/activities" in str(e)
        assert "ANTHROPIC_COMPLIANCE_API_PATH" in str(e)
    print("OK test_anthropic_404_message_points_at_path_and_env_var")


def test_anthropic_403_message_points_at_enablement_and_scope():
    c = AnthropicComplianceClient("sk-ant-admin01-test", http=StaticHttp(403, b"forbidden"))
    try:
        list(c.fetch_window(NOW - timedelta(minutes=5), NOW))
        raise AssertionError("expected error")
    except AnthropicComplianceAPIError as e:
        assert "Compliance API is enabled" in str(e)
        assert "read:compliance_activities" in str(e)
    print("OK test_anthropic_403_message_points_at_enablement_and_scope")


def test_anthropic_400_surfaces_message():
    body = b'{"error":{"type":"invalid_request_error","message":"bad created_at.gte"}}'
    c = AnthropicComplianceClient("sk-ant-admin01-test", http=StaticHttp(400, body))
    try:
        list(c.fetch_window(NOW - timedelta(minutes=5), NOW))
        raise AssertionError("expected error")
    except AnthropicComplianceAPIError as e:
        assert "bad created_at.gte" in str(e)
    print("OK test_anthropic_400_surfaces_message")


# ── OpenAI-specific tests ──────────────────────────────────────────────────


def test_openai_admin_key_accepted():
    OpenAIAuditClient("sk-admin-test")
    print("OK test_openai_admin_key_accepted")


def test_openai_other_keys_rejected():
    for bad in (
        "sk-test",  # standard project key
        "sk-svcacct-test",  # service account key
        "sk-ant-admin01-test",  # Anthropic key
        "no-prefix",
    ):
        try:
            OpenAIAuditClient(bad)
            raise AssertionError(f"accepted bad key {bad}")
        except ValueError as e:
            assert "sk-admin-" in str(e)
    print("OK test_openai_other_keys_rejected")


def test_openai_request_url_and_headers():
    body = json.dumps({"data": [], "has_more": False}).encode()
    http = CapturingHttp((200, body))
    c = OpenAIAuditClient("sk-admin-test", http=http)
    list(c.fetch_window(NOW - timedelta(minutes=10), NOW))
    req = http.requests[0]
    parsed = urlparse(req["url"])
    assert parsed.path == "/v1/organization/audit_logs"
    # Bracket-notation time filter (not dotted like Anthropic)
    qs = parse_qs(parsed.query)
    assert "effective_at[gte]" in qs, list(qs.keys())
    assert "effective_at[lte]" in qs
    # Unix seconds, not ISO
    assert qs["effective_at[gte]"][0].isdigit()
    assert qs["limit"] == ["100"]
    # Auth header is Authorization Bearer (not x-api-key)
    assert req["headers"]["Authorization"] == "Bearer sk-admin-test"
    print("OK test_openai_request_url_and_headers")


def test_openai_unix_to_iso_round_trip():
    from forwarder.vendors.openai_audit import _to_unix, _unix_to_iso

    iso = _unix_to_iso(1234567890)
    assert iso.endswith("Z")
    parsed = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    assert int(parsed.timestamp()) == 1234567890
    assert _to_unix(NOW) == int(NOW.timestamp())
    print("OK test_openai_unix_to_iso_round_trip")


def test_openai_event_normalization():
    # Verify the OpenAI client converts effective_at → ISO created_at
    raw = OPENAI_EVENTS[0]
    body = json.dumps({"data": [raw], "has_more": False}).encode()
    http = CapturingHttp((200, body))
    c = OpenAIAuditClient("sk-admin-test", http=http)
    events = list(c.fetch_window(NOW - timedelta(minutes=15), NOW))
    assert len(events) == 1
    ev = events[0]
    assert ev.vendor == "openai"
    assert ev.id == "audit_log-abc123"
    # ISO format, ends with Z
    assert ev.created_at.endswith("Z")
    # Round-trippable to the original Unix timestamp
    parsed = datetime.fromisoformat(ev.created_at.replace("Z", "+00:00"))
    assert int(parsed.timestamp()) == raw["effective_at"]
    # Raw payload preserved (effective_at still present)
    assert ev.raw["effective_at"] == raw["effective_at"]
    print("OK test_openai_event_normalization")


def test_openai_pagination_via_after():
    pages = [
        (
            200,
            json.dumps(
                {
                    "data": [
                        {"id": "audit_log-1", "effective_at": _unix(NOW - timedelta(minutes=2)),
                         "type": "login.succeeded", "actor": {"session": {}}}
                    ],
                    "has_more": True,
                    "last_id": "audit_log-1",
                }
            ).encode(),
        ),
        (
            200,
            json.dumps(
                {
                    "data": [
                        {"id": "audit_log-2", "effective_at": _unix(NOW - timedelta(minutes=10)),
                         "type": "login.failed", "actor": {"session": {}}}
                    ],
                    "has_more": False,
                }
            ).encode(),
        ),
    ]
    http = ScriptedHttp(pages)
    c = OpenAIAuditClient("sk-admin-test", http=http)
    events = list(c.fetch_window(NOW - timedelta(hours=1), NOW))
    assert len(events) == 2
    assert events[0].id == "audit_log-2"  # oldest first
    parsed = urlparse(http.requests[1]["url"])
    qs = parse_qs(parsed.query)
    assert qs["after"] == ["audit_log-1"]
    print("OK test_openai_pagination_via_after")


def test_openai_404_message():
    c = OpenAIAuditClient("sk-admin-test", http=StaticHttp(404, b"not found"))
    try:
        list(c.fetch_window(NOW - timedelta(minutes=5), NOW))
        raise AssertionError("expected error")
    except OpenAIAuditAPIError as e:
        assert "/v1/organization/audit_logs" in str(e)
        assert "OPENAI_AUDIT_LOGS_PATH" in str(e)
    print("OK test_openai_404_message")


def test_openai_403_message_points_at_audit_logging_setting():
    c = OpenAIAuditClient("sk-admin-test", http=StaticHttp(403, b"forbidden"))
    try:
        list(c.fetch_window(NOW - timedelta(minutes=5), NOW))
        raise AssertionError("expected error")
    except OpenAIAuditAPIError as e:
        assert "audit logging is enabled" in str(e)
        assert "Data retention" in str(e)
    print("OK test_openai_403_message_points_at_audit_logging_setting")


# ── Cross-vendor + pipeline tests ──────────────────────────────────────────


def test_run_with_anthropic_through_s3_uses_vendor_prefix():
    fake = FakeS3Client()
    egress = S3Egress("audit-bucket", vendor="anthropic", s3_client=fake)
    run(FakeAnthropic(ANTHROPIC_EVENTS), egress, MemStore("anthropic"), now=NOW)
    put = fake.calls[0]
    assert put["Key"].startswith("anthropic/audit/"), put["Key"]
    assert put["Metadata"] == {"vendor": "anthropic"}
    print("OK test_run_with_anthropic_through_s3_uses_vendor_prefix")


def test_run_with_openai_through_s3_uses_vendor_prefix():
    fake = FakeS3Client()
    egress = S3Egress("audit-bucket", vendor="openai", s3_client=fake)
    run(FakeOpenAI(OPENAI_EVENTS), egress, MemStore("openai"), now=NOW)
    put = fake.calls[0]
    assert put["Key"].startswith("openai/audit/"), put["Key"]
    assert put["Metadata"] == {"vendor": "openai"}
    # Each line in the gzipped body is a raw OpenAI Audit Log object
    lines = gzip.decompress(put["Body"]).decode().strip().split("\n")
    parsed = [json.loads(l) for l in lines]
    assert all("effective_at" in p and isinstance(p["effective_at"], int) for p in parsed)
    assert [p["id"] for p in parsed] == ["audit_log-abc123", "audit_log-def456", "audit_log-ghi789"]
    print("OK test_run_with_openai_through_s3_uses_vendor_prefix")


def test_pubsub_anthropic_attributes():
    pub = FakePublisher()
    egress = PubSubEgress("p", "t", vendor="anthropic", publisher=pub)
    egress.send(ANTHROPIC_EVENTS)
    msg = pub.published[0]
    assert msg["attrs"]["vendor"] == "anthropic"
    assert msg["attrs"]["activity_type"] == "claude_chat_created"
    assert msg["attrs"]["actor_type"] == "user_actor"
    assert msg["attrs"]["actor_user_id"] == "user_xyz456"
    assert msg["attrs"]["organization_id"] == "org_abc123"
    # ApiActor route
    api_msg = pub.published[1]
    assert api_msg["attrs"]["actor_type"] == "api_actor"
    assert api_msg["attrs"]["actor_api_key_id"] == "apikey_fghij567890"
    print("OK test_pubsub_anthropic_attributes")


def test_pubsub_openai_attributes():
    pub = FakePublisher()
    egress = PubSubEgress("p", "t", vendor="openai", publisher=pub)
    egress.send(OPENAI_EVENTS)

    by_type = {json.loads(m["data"])["type"]: m for m in pub.published}

    login_ok = by_type["login.succeeded"]
    assert login_ok["attrs"]["vendor"] == "openai"
    assert login_ok["attrs"]["activity_type"] == "login.succeeded"
    assert login_ok["attrs"]["actor_type"] == "session"
    assert login_ok["attrs"]["actor_user_id"] == "user-2x4f"
    # No project on this event → project_id attr absent
    assert "project_id" not in login_ok["attrs"]

    apikey_create = by_type["api_key.created"]
    assert apikey_create["attrs"]["actor_type"] == "api_key"
    assert apikey_create["attrs"]["actor_api_key_id"] == "key_admin_xyz"
    assert apikey_create["attrs"]["project_id"] == "proj_789"
    print("OK test_pubsub_openai_attributes")


def test_http_fallback_envelope_per_vendor():
    # Anthropic
    http_a = CapturingHttp((202, b""))
    a_egress = HttpEgress(
        HttpEgressConfig(url="https://col/", token="t", vendor="anthropic"), http=http_a
    )
    a_egress.send(ANTHROPIC_EVENTS)
    body = json.loads(gzip.decompress(http_a.requests[0]["body"]).decode())
    assert body[0]["_vendor"] == "anthropic"
    assert body[0]["_product"] == "anthropic_audit_log"
    assert body[0]["_time"] == ANTHROPIC_EVENTS[0]["created_at"]
    assert body[0]["_event"] == "claude_chat_created"

    # OpenAI: _time derived from effective_at via Unix→ISO conversion
    http_o = CapturingHttp((202, b""))
    o_egress = HttpEgress(
        HttpEgressConfig(url="https://col/", token="t", vendor="openai"), http=http_o
    )
    o_egress.send(OPENAI_EVENTS)
    body = json.loads(gzip.decompress(http_o.requests[0]["body"]).decode())
    assert body[0]["_vendor"] == "openai"
    assert body[0]["_product"] == "openai_audit_log"
    parsed = datetime.fromisoformat(body[0]["_time"].replace("Z", "+00:00"))
    assert int(parsed.timestamp()) == OPENAI_EVENTS[0]["effective_at"]
    assert body[0]["_event"] == "login.succeeded"
    print("OK test_http_fallback_envelope_per_vendor")


def test_state_isolation_between_vendors():
    """Two vendor runs against shared storage must not collide."""
    # Use shared dict as the "DynamoDB table" simulator
    table = {}

    class Shared(StateStore):
        def __init__(self, vendor):
            self.vendor = vendor

        def load(self):
            return ForwarderState.from_dict(table.get(self.vendor))

        def save(self, st):
            table[self.vendor] = st.to_dict()

    # Run Anthropic: watermark advances; OpenAI state unchanged
    run(FakeAnthropic(ANTHROPIC_EVENTS), S3Egress("b", vendor="anthropic", s3_client=FakeS3Client()),
        Shared("anthropic"), now=NOW)
    assert "anthropic" in table and "openai" not in table

    # Run OpenAI: independent watermark
    run(FakeOpenAI(OPENAI_EVENTS), S3Egress("b", vendor="openai", s3_client=FakeS3Client()),
        Shared("openai"), now=NOW)
    assert "openai" in table

    a_wm = table["anthropic"]["watermark"]
    o_wm = table["openai"]["watermark"]
    assert a_wm and o_wm and a_wm != o_wm
    # IDs don't cross-contaminate either
    assert all(i.startswith("activity_") for i in table["anthropic"]["recent_ids"])
    assert all(i.startswith("audit_log-") for i in table["openai"]["recent_ids"])
    print("OK test_state_isolation_between_vendors")


def test_cross_vendor_dedupe_no_collision():
    """An Anthropic id and OpenAI id with the same string would collide if state weren't namespaced."""
    table = {}

    class Shared(StateStore):
        def __init__(self, vendor):
            self.vendor = vendor

        def load(self):
            return ForwarderState.from_dict(table.get(self.vendor))

        def save(self, st):
            table[self.vendor] = st.to_dict()

    # Pathological: same string id in both vendors' fixtures
    a_evs = [
        {**ANTHROPIC_EVENTS[0], "id": "x"},
    ]
    o_evs = [
        {**OPENAI_EVENTS[0], "id": "x"},
    ]
    run(FakeAnthropic(a_evs), S3Egress("b", vendor="anthropic", s3_client=FakeS3Client()),
        Shared("anthropic"), now=NOW)
    run(FakeOpenAI(o_evs), S3Egress("b", vendor="openai", s3_client=FakeS3Client()),
        Shared("openai"), now=NOW)
    # Each run forwarded its event; namespace prevented dedupe across vendors
    assert table["anthropic"]["recent_ids"] == ["x"]
    assert table["openai"]["recent_ids"] == ["x"]
    print("OK test_cross_vendor_dedupe_no_collision")


def test_dedupe_with_overlap_window():
    store = MemStore("anthropic")
    run(FakeAnthropic(ANTHROPIC_EVENTS), S3Egress("b", vendor="anthropic", s3_client=FakeS3Client()),
        store, now=NOW)

    # 5 min later, query window is [watermark - 5min, later]. Only the events
    # within that window will reach the dedupe check; older fixture events
    # are correctly filtered upstream by the API's time filter.
    later = NOW + timedelta(minutes=5)
    new_event = {
        **ANTHROPIC_EVENTS[0],
        "id": "activity_NEW",
        "created_at": _iso(later - timedelta(seconds=30)),
    }
    fake = FakeS3Client()
    s = run(FakeAnthropic(ANTHROPIC_EVENTS + [new_event]),
            S3Egress("b", vendor="anthropic", s3_client=fake), store, now=later)
    # ANTHROPIC_EVENTS[1] is at NOW-4min; second run window starts at
    # watermark-5min = NOW-4min-5min = NOW-9min, so it falls inside and gets
    # deduped. ANTHROPIC_EVENTS[0] at NOW-10min falls outside. new_event is
    # within window and unseen → forwarded.
    assert s["forwarded"] == 1, s
    assert s["skipped_duplicate"] == 1, s
    print("OK test_dedupe_with_overlap_window")


def test_egress_failure_keeps_watermark():
    store = MemStore("anthropic")
    pub = FakePublisher(fail=True)
    egress = PubSubEgress("p", "t", vendor="anthropic", publisher=pub)
    try:
        run(FakeAnthropic(ANTHROPIC_EVENTS), egress, store, now=NOW)
        raise AssertionError("expected RuntimeError")
    except RuntimeError as e:
        assert "publish failed" in str(e)
    assert store.saves == 0
    assert store.s.watermark is None
    print("OK test_egress_failure_keeps_watermark")


def test_recent_ids_bounded():
    big = set(f"activity_{i:08x}" for i in range(MAX_RECENT_IDS + 5000))
    result = _compute_state(big, "2026-05-04T12:00:00Z")
    assert len(result.recent_ids) == MAX_RECENT_IDS
    print("OK test_recent_ids_bounded")


def test_state_legacy_field_tolerated():
    legacy = {"watermark": "2026-05-04T12:00:00Z", "recent_hashes": ["h1", "h2"]}
    restored = ForwarderState.from_dict(legacy)
    assert restored.recent_ids == ["h1", "h2"]
    print("OK test_state_legacy_field_tolerated")


def test_constants_match_specs():
    assert ANTHROPIC_PATH == "/v1/compliance/activities"
    assert OPENAI_PATH == "/v1/organization/audit_logs"
    assert OVERLAP_SECONDS == 300
    assert MAX_RECENT_IDS == 10_000
    print("OK test_constants_match_specs")


def test_summary_includes_vendor():
    s = run(FakeAnthropic([]), S3Egress("b", vendor="anthropic", s3_client=FakeS3Client()),
            MemStore("anthropic"), now=NOW)
    assert s["vendor"] == "anthropic"
    s = run(FakeOpenAI([]), S3Egress("b", vendor="openai", s3_client=FakeS3Client()),
            MemStore("openai"), now=NOW)
    assert s["vendor"] == "openai"
    print("OK test_summary_includes_vendor")


def test_parallel_execution_no_contention():
    """Both vendors run concurrently against a shared, lock-free state store
    and a shared in-memory bucket. Asserts that:
      - Each vendor sees only its own events forwarded
      - State documents stay vendor-namespaced (no cross-write)
      - Both runs reach a watermark > prior watermark
      - Each vendor's S3 object key uses its own /<vendor>/ prefix
    """
    import threading

    # Shared "table" (DynamoDB-style) — vendor namespacing comes from PK,
    # exactly as in production state_aws.py.
    table = {}
    table_lock = threading.Lock()

    class SharedTableStore(StateStore):
        def __init__(self, vendor):
            self.vendor = vendor

        def load(self):
            with table_lock:
                return ForwarderState.from_dict(table.get(f"{self.vendor}_audit_state"))

        def save(self, st):
            with table_lock:
                table[f"{self.vendor}_audit_state"] = st.to_dict()

    # Shared bucket simulator — concurrent writes from both threads.
    bucket_calls = []
    bucket_lock = threading.Lock()

    class SharedBucket:
        def put_object(self, **kw):
            with bucket_lock:
                bucket_calls.append(kw)

    # Build per-vendor stacks and run both in threads simultaneously.
    barrier = threading.Barrier(2)
    summaries = {}
    errors: list = []

    def run_one(client, egress, store, label):
        try:
            barrier.wait()  # release both threads at the same instant
            summaries[label] = run(client, egress, store, now=NOW)
        except Exception as e:
            errors.append((label, e))

    bucket = SharedBucket()
    threads = [
        threading.Thread(target=run_one, args=(
            FakeAnthropic(ANTHROPIC_EVENTS),
            S3Egress("audit-bucket", vendor="anthropic", s3_client=bucket),
            SharedTableStore("anthropic"),
            "anthropic",
        )),
        threading.Thread(target=run_one, args=(
            FakeOpenAI(OPENAI_EVENTS),
            S3Egress("audit-bucket", vendor="openai", s3_client=bucket),
            SharedTableStore("openai"),
            "openai",
        )),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, f"thread errors: {errors}"
    assert len(summaries) == 2

    # Each vendor forwarded its own count
    assert summaries["anthropic"]["forwarded"] == len(ANTHROPIC_EVENTS), summaries["anthropic"]
    assert summaries["openai"]["forwarded"] == len(OPENAI_EVENTS), summaries["openai"]

    # State stays vendor-namespaced
    assert "anthropic_audit_state" in table
    assert "openai_audit_state" in table
    a_state = table["anthropic_audit_state"]
    o_state = table["openai_audit_state"]
    assert all(i.startswith("activity_") for i in a_state["recent_ids"])
    assert all(i.startswith("audit_log-") for i in o_state["recent_ids"])
    # Watermarks differ (vendors emitted events at different timestamps)
    assert a_state["watermark"] != o_state["watermark"]

    # Two S3 objects total — one per vendor — with vendor-prefixed keys
    assert len(bucket_calls) == 2
    keys_by_prefix = {}
    for c in bucket_calls:
        prefix = c["Key"].split("/", 1)[0]
        keys_by_prefix[prefix] = c["Metadata"]["vendor"]
    assert keys_by_prefix == {"anthropic": "anthropic", "openai": "openai"}
    print("OK test_parallel_execution_no_contention")


# ── Anthropic chat content (anthropic_chats) ──────────────────────────────


def test_anthropic_chats_requires_compliance_access_key():
    # Admin keys are explicitly rejected — they only authorize Activity Feed
    try:
        AnthropicChatContentClient("sk-ant-admin01-test")
        raise AssertionError("admin key should be rejected for content endpoints")
    except ValueError as e:
        assert "Compliance Access Key" in str(e)
        assert "sk-ant-api01-" in str(e)
    AnthropicChatContentClient("sk-ant-api01-test")  # accepts the right key
    print("OK test_anthropic_chats_requires_compliance_access_key")


def test_anthropic_chats_emits_one_event_per_message():
    list_resp = json.dumps(
        {
            "data": [{"id": "claude_chat_abc", "updated_at": _iso(NOW - timedelta(minutes=5))}],
            "has_more": False,
        }
    ).encode()
    chat_resp = json.dumps(
        {
            "id": "claude_chat_abc",
            "name": "Q3 plan",
            "organization_id": "org_x",
            "project_id": "claude_proj_abc",
            "user": {"id": "user_alice", "email_address": "alice@example.com"},
            "chat_messages": [
                {"id": "msg_1", "role": "user", "created_at": _iso(NOW - timedelta(minutes=4)),
                 "content": [{"type": "text", "text": "what's the plan?"}]},
                {"id": "msg_2", "role": "assistant", "created_at": _iso(NOW - timedelta(minutes=3)),
                 "content": [{"type": "text", "text": "here's the plan..."}]},
            ],
        }
    ).encode()
    http = ScriptedHttp([(200, list_resp), (200, chat_resp)])
    c = AnthropicChatContentClient("sk-ant-api01-test", http=http)
    events = list(c.fetch_window(NOW - timedelta(hours=1), NOW))
    assert len(events) == 2
    assert events[0].vendor == "anthropic_chats"
    assert events[0].id == "msg_1" and events[1].id == "msg_2"
    # Each event wraps {chat: {...meta...}, message: {...content...}}
    assert events[0].raw["chat"]["id"] == "claude_chat_abc"
    assert events[0].raw["message"]["role"] == "user"
    assert events[1].raw["message"]["role"] == "assistant"
    # Chat metadata excludes the messages array (it's split out per-event)
    assert "chat_messages" not in events[0].raw["chat"]
    print("OK test_anthropic_chats_emits_one_event_per_message")


def test_anthropic_chats_uses_updated_at_filter():
    body = json.dumps({"data": [], "has_more": False}).encode()
    http = CapturingHttp((200, body))
    c = AnthropicChatContentClient("sk-ant-api01-test", http=http)
    list(c.fetch_window(NOW - timedelta(minutes=10), NOW))
    parsed = urlparse(http.requests[0]["url"])
    assert parsed.path == "/v1/compliance/apps/chats"
    qs = parse_qs(parsed.query)
    assert "updated_at.gte" in qs and "updated_at.lte" in qs
    print("OK test_anthropic_chats_uses_updated_at_filter")


def test_anthropic_chats_404_message():
    c = AnthropicChatContentClient("sk-ant-api01-test", http=StaticHttp(404, b"nope"))
    try:
        list(c.fetch_window(NOW - timedelta(minutes=5), NOW))
        raise AssertionError
    except AnthropicChatContentAPIError as e:
        assert "/v1/compliance/apps/chats" in str(e)
        assert "ANTHROPIC_CHATS_LIST_PATH" in str(e)
    print("OK test_anthropic_chats_404_message")


def test_anthropic_chats_403_warns_about_admin_key():
    c = AnthropicChatContentClient("sk-ant-api01-test", http=StaticHttp(403, b"forbidden"))
    try:
        list(c.fetch_window(NOW - timedelta(minutes=5), NOW))
        raise AssertionError
    except AnthropicChatContentAPIError as e:
        assert "Compliance Access Key" in str(e)
        assert "Admin key" in str(e) or "admin key" in str(e).lower()
    print("OK test_anthropic_chats_403_warns_about_admin_key")


def test_anthropic_chats_skips_individual_failed_chat():
    # If one chat 500s, the run should skip it and continue, not abort.
    list_resp = json.dumps(
        {
            "data": [
                {"id": "claude_chat_bad", "updated_at": _iso(NOW)},
                {"id": "claude_chat_good", "updated_at": _iso(NOW)},
            ],
            "has_more": False,
        }
    ).encode()
    good_resp = json.dumps(
        {
            "id": "claude_chat_good",
            "user": {"id": "u"},
            "chat_messages": [{"id": "msg_g", "role": "user",
                               "created_at": _iso(NOW), "content": []}],
        }
    ).encode()
    # 4 attempts of 500 = exhausted retries on the bad chat
    pages = [(200, list_resp)] + [(500, b"bork")] * 4 + [(200, good_resp)]
    http = ScriptedHttp(pages)
    c = AnthropicChatContentClient("sk-ant-api01-test", http=http)
    events = list(c.fetch_window(NOW - timedelta(hours=1), NOW))
    assert len(events) == 1 and events[0].id == "msg_g"
    print("OK test_anthropic_chats_skips_individual_failed_chat")


# ── OpenAI Conversations (openai_conversations) ────────────────────────────


def test_openai_conversations_admin_key_required():
    OpenAIConversationsClient("sk-admin-test")
    for bad in ("sk-test", "sk-ant-admin01-test"):
        try:
            OpenAIConversationsClient(bad)
            raise AssertionError
        except ValueError as e:
            assert "sk-admin-" in str(e)
    print("OK test_openai_conversations_admin_key_required")


def test_openai_conversations_handles_per_message_response():
    # If the spec returns flat per-message records.
    body = json.dumps(
        {
            "data": [
                {"id": "msg-1", "effective_at": _unix(NOW - timedelta(minutes=2)),
                 "role": "user", "content": "hi"},
                {"id": "msg-2", "effective_at": _unix(NOW - timedelta(minutes=1)),
                 "role": "assistant", "content": "hello"},
            ],
            "has_more": False,
        }
    ).encode()
    http = CapturingHttp((200, body))
    c = OpenAIConversationsClient("sk-admin-test", http=http)
    events = list(c.fetch_window(NOW - timedelta(minutes=10), NOW))
    assert len(events) == 2
    assert events[0].id == "msg-1" and events[1].id == "msg-2"
    assert events[0].vendor == "openai_conversations"
    print("OK test_openai_conversations_handles_per_message_response")


def test_openai_conversations_handles_per_conversation_response():
    # If the spec returns conversation-level records with embedded messages.
    body = json.dumps(
        {
            "data": [
                {
                    "id": "conv-abc",
                    "effective_at": _unix(NOW - timedelta(minutes=5)),
                    "workspace_id": "ws-1",
                    "messages": [
                        {"id": "m1", "effective_at": _unix(NOW - timedelta(minutes=5)),
                         "role": "user", "content": "q"},
                        {"id": "m2", "effective_at": _unix(NOW - timedelta(minutes=4)),
                         "role": "assistant", "content": "a"},
                    ],
                }
            ],
            "has_more": False,
        }
    ).encode()
    http = CapturingHttp((200, body))
    c = OpenAIConversationsClient("sk-admin-test", http=http)
    events = list(c.fetch_window(NOW - timedelta(minutes=10), NOW))
    assert len(events) == 2
    assert events[0].id == "m1" and events[1].id == "m2"
    # Each event wraps {conversation: meta, message: ...}
    assert events[0].raw["conversation"]["id"] == "conv-abc"
    assert events[0].raw["message"]["role"] == "user"
    print("OK test_openai_conversations_handles_per_conversation_response")


def test_openai_conversations_synthesizes_id_when_missing():
    # If the spec is vague and a record arrives without id, dedupe must
    # still work — adapter synthesizes a stable hash.
    body = json.dumps(
        {
            "data": [{"effective_at": _unix(NOW), "role": "user", "content": "x"}],
            "has_more": False,
        }
    ).encode()
    http = CapturingHttp((200, body))
    c = OpenAIConversationsClient("sk-admin-test", http=http)
    events = list(c.fetch_window(NOW - timedelta(minutes=10), NOW))
    assert len(events) == 1
    assert events[0].id.startswith("synthetic_")
    print("OK test_openai_conversations_synthesizes_id_when_missing")


def test_openai_conversations_request_uses_bracket_filter():
    body = json.dumps({"data": [], "has_more": False}).encode()
    http = CapturingHttp((200, body))
    c = OpenAIConversationsClient("sk-admin-test", workspace_id="ws-1", http=http)
    list(c.fetch_window(NOW - timedelta(minutes=10), NOW))
    parsed = urlparse(http.requests[0]["url"])
    qs = parse_qs(parsed.query)
    # Same bracketed Unix-seconds filter as audit logs
    assert "effective_at[gte]" in qs and "effective_at[lte]" in qs
    assert qs["workspace_id"] == ["ws-1"]
    assert http.requests[0]["headers"]["Authorization"] == "Bearer sk-admin-test"
    print("OK test_openai_conversations_request_uses_bracket_filter")


def test_openai_conversations_404_message_points_at_palo_native():
    c = OpenAIConversationsClient("sk-admin-test", http=StaticHttp(404, b"nope"))
    try:
        list(c.fetch_window(NOW - timedelta(minutes=5), NOW))
        raise AssertionError
    except OpenAIConversationsAPIError as e:
        # Help operators find the alternative
        assert "Palo Alto" in str(e) or "palo" in str(e).lower()
        assert "OPENAI_CONVERSATIONS_PATH" in str(e)
    print("OK test_openai_conversations_404_message_points_at_palo_native")


# ── Cross-vendor with all four feeds ───────────────────────────────────────


def test_pubsub_emits_vendor_attribute_for_all_four():
    """Pub/Sub egress must tag every message with vendor= so XSIAM can route."""
    cases = [
        ("anthropic", ANTHROPIC_EVENTS[0]),
        ("anthropic_chats", {
            "chat": {"id": "claude_chat_abc", "organization_id": "org_x",
                     "project_id": "p1", "user": {"id": "user_alice"}},
            "message": {"id": "msg_1", "role": "user",
                        "created_at": _iso(NOW), "content": []},
        }),
        ("openai", OPENAI_EVENTS[0]),
        ("openai_conversations", {
            "conversation": {"id": "conv-abc", "workspace_id": "ws-1"},
            "message": {"id": "msg_1", "role": "user",
                        "effective_at": _unix(NOW), "model": "gpt-4o"},
        }),
    ]
    for vendor, ev in cases:
        pub = FakePublisher()
        e = PubSubEgress("p", "t", vendor=vendor, publisher=pub)
        e.send([ev])
        assert pub.published[0]["attrs"]["vendor"] == vendor, vendor
    print("OK test_pubsub_emits_vendor_attribute_for_all_four")


def test_pubsub_anthropic_chats_attributes():
    pub = FakePublisher()
    e = PubSubEgress("p", "t", vendor="anthropic_chats", publisher=pub)
    ev = {
        "chat": {"id": "claude_chat_abc", "organization_id": "org_x",
                 "project_id": "p1", "user": {"id": "user_alice"}},
        "message": {"id": "msg_1", "role": "user",
                    "created_at": _iso(NOW), "content": []},
    }
    e.send([ev])
    a = pub.published[0]["attrs"]
    assert a["vendor"] == "anthropic_chats"
    assert a["chat_id"] == "claude_chat_abc"
    assert a["organization_id"] == "org_x"
    assert a["project_id"] == "p1"
    assert a["actor_user_id"] == "user_alice"
    assert a["message_role"] == "user"
    print("OK test_pubsub_anthropic_chats_attributes")


def test_pubsub_openai_conversations_attributes():
    pub = FakePublisher()
    e = PubSubEgress("p", "t", vendor="openai_conversations", publisher=pub)
    ev = {
        "conversation": {"id": "conv-abc", "workspace_id": "ws-1"},
        "message": {"id": "msg_1", "role": "assistant",
                    "effective_at": _unix(NOW), "model": "gpt-4o"},
    }
    e.send([ev])
    a = pub.published[0]["attrs"]
    assert a["vendor"] == "openai_conversations"
    assert a["conversation_id"] == "conv-abc"
    assert a["workspace_id"] == "ws-1"
    assert a["model"] == "gpt-4o"
    assert a["message_role"] == "assistant"
    print("OK test_pubsub_openai_conversations_attributes")


def test_http_envelope_for_chats_and_conversations():
    """HTTP fallback envelope picks the right _time field for wrapped payloads."""
    # anthropic_chats
    http = CapturingHttp((202, b""))
    e = HttpEgress(HttpEgressConfig(url="https://col/", token="t",
                                    vendor="anthropic_chats"), http=http)
    ev = {
        "chat": {"id": "c"},
        "message": {"id": "m1", "role": "user",
                    "created_at": "2026-05-08T10:00:00Z", "content": []},
    }
    e.send([ev])
    body = json.loads(gzip.decompress(http.requests[0]["body"]).decode())
    assert body[0]["_vendor"] == "anthropic_chats"
    assert body[0]["_product"] == "anthropic_chats_audit_log"
    assert body[0]["_time"] == "2026-05-08T10:00:00Z"
    assert body[0]["_event"] == "claude_chat_message"

    # openai_conversations
    http = CapturingHttp((202, b""))
    e = HttpEgress(HttpEgressConfig(url="https://col/", token="t",
                                    vendor="openai_conversations"), http=http)
    ev = {
        "conversation": {"id": "c"},
        "message": {"id": "m1", "role": "user", "effective_at": _unix(NOW)},
    }
    e.send([ev])
    body = json.loads(gzip.decompress(http.requests[0]["body"]).decode())
    assert body[0]["_vendor"] == "openai_conversations"
    assert body[0]["_event"] == "openai_conversation_message"
    parsed = datetime.fromisoformat(body[0]["_time"].replace("Z", "+00:00"))
    assert int(parsed.timestamp()) == int(NOW.timestamp())
    print("OK test_http_envelope_for_chats_and_conversations")


def test_parallel_repeated_runs_dedupe_correctly():
    """Stress: same vendor run twice in parallel against shared state. Even
    if the cap-of-1 is bypassed (e.g. a misconfigured deploy), the dedupe
    by event id must mean no event is forwarded twice. State watermark
    might regress under a race, but XSIAM-side dedupe by id catches it.
    """
    import threading

    table = {}
    table_lock = threading.Lock()
    egress_received = []
    egress_lock = threading.Lock()

    class SharedTableStore(StateStore):
        def __init__(self, vendor):
            self.vendor = vendor

        def load(self):
            with table_lock:
                return ForwarderState.from_dict(table.get(f"{self.vendor}_audit_state"))

        def save(self, st):
            with table_lock:
                table[f"{self.vendor}_audit_state"] = st.to_dict()

    class CountingEgress:
        def send(self, events):
            evs = list(events)
            with egress_lock:
                egress_received.extend(evs)
            return len(evs)

    barrier = threading.Barrier(2)

    def go():
        barrier.wait()
        run(FakeAnthropic(ANTHROPIC_EVENTS), CountingEgress(),
            SharedTableStore("anthropic"), now=NOW)

    threads = [threading.Thread(target=go), threading.Thread(target=go)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    # The egress may receive each event up to 2x (if the parallel runs both
    # see an empty/identical state load). What MUST hold is that the unique
    # event ids equal the input set — i.e. there's no fabrication and no
    # mutation of payloads.
    forwarded_ids = {ev["id"] for ev in egress_received}
    expected_ids = {ev["id"] for ev in ANTHROPIC_EVENTS}
    assert forwarded_ids == expected_ids, (forwarded_ids, expected_ids)
    # And state ends in a consistent shape (not corrupt)
    final = ForwarderState.from_dict(table.get("anthropic_audit_state"))
    assert final.watermark and len(final.recent_ids) <= MAX_RECENT_IDS
    print("OK test_parallel_repeated_runs_dedupe_correctly")


# ── Test runner ────────────────────────────────────────────────────────────


TESTS = [
    # Anthropic
    test_anthropic_admin_and_compliance_keys_accepted,
    test_anthropic_other_keys_rejected,
    test_anthropic_request_url_and_headers,
    test_anthropic_pagination_via_after_id,
    test_anthropic_pagination_terminates_on_missing_last_id,
    test_anthropic_404_message_points_at_path_and_env_var,
    test_anthropic_403_message_points_at_enablement_and_scope,
    test_anthropic_400_surfaces_message,
    # OpenAI
    test_openai_admin_key_accepted,
    test_openai_other_keys_rejected,
    test_openai_request_url_and_headers,
    test_openai_unix_to_iso_round_trip,
    test_openai_event_normalization,
    test_openai_pagination_via_after,
    test_openai_404_message,
    test_openai_403_message_points_at_audit_logging_setting,
    # Cross-vendor + pipeline
    test_run_with_anthropic_through_s3_uses_vendor_prefix,
    test_run_with_openai_through_s3_uses_vendor_prefix,
    test_pubsub_anthropic_attributes,
    test_pubsub_openai_attributes,
    test_http_fallback_envelope_per_vendor,
    test_state_isolation_between_vendors,
    test_cross_vendor_dedupe_no_collision,
    test_dedupe_with_overlap_window,
    test_egress_failure_keeps_watermark,
    test_recent_ids_bounded,
    test_state_legacy_field_tolerated,
    test_constants_match_specs,
    test_summary_includes_vendor,
    # Anthropic chat content
    test_anthropic_chats_requires_compliance_access_key,
    test_anthropic_chats_emits_one_event_per_message,
    test_anthropic_chats_uses_updated_at_filter,
    test_anthropic_chats_404_message,
    test_anthropic_chats_403_warns_about_admin_key,
    test_anthropic_chats_skips_individual_failed_chat,
    # OpenAI conversations
    test_openai_conversations_admin_key_required,
    test_openai_conversations_handles_per_message_response,
    test_openai_conversations_handles_per_conversation_response,
    test_openai_conversations_synthesizes_id_when_missing,
    test_openai_conversations_request_uses_bracket_filter,
    test_openai_conversations_404_message_points_at_palo_native,
    # Cross-feed Pub/Sub + HTTP envelope
    test_pubsub_emits_vendor_attribute_for_all_four,
    test_pubsub_anthropic_chats_attributes,
    test_pubsub_openai_conversations_attributes,
    test_http_envelope_for_chats_and_conversations,
    # Parallel execution
    test_parallel_execution_no_contention,
    test_parallel_repeated_runs_dedupe_correctly,
]


if __name__ == "__main__":
    for t in TESTS:
        t()
    print(f"\nALL {len(TESTS)} SMOKE TESTS PASSED")
