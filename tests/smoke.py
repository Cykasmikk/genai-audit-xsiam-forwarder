"""Smoke test for the forwarder core, S3 egress, and Pub/Sub egress.

Runs in CI without AWS or GCP credentials by injecting fake low-level
clients. Validates against Compliance API Rev J Activity schema:
  - Module imports against real boto3 / google-cloud-pubsub SDKs
  - First-run + subsequent-run watermark + activity-id dedupe
  - S3 object format (gzipped JSON-lines, AES256, content type)
  - Pub/Sub message format (per-event, attributes for routing)
  - Pub/Sub publish failure aborts the run before watermark advance
  - Compliance API client error handling (404, 403, key-prefix validation)
  - State bound at MAX_RECENT_IDS
  - Both Admin keys and Compliance Access Keys accepted
"""

from __future__ import annotations

import gzip
import json
from datetime import datetime, timedelta, timezone

from forwarder.aws_handler import handler as aws_handler  # noqa: F401
from forwarder.claude_client import (
    ActivityEvent,
    COMPLIANCE_API_PATH,
    ClaudeComplianceClient,
    ComplianceAPIError,
)
from forwarder.core import MAX_RECENT_IDS, OVERLAP_SECONDS, _compute_state, run
from forwarder.egress.pubsub import PubSubEgress
from forwarder.egress.s3 import S3Egress
from forwarder.gcp_handler import handler as gcp_handler  # noqa: F401
from forwarder.state import ForwarderState, StateStore


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


class MemStore(StateStore):
    def __init__(self):
        self.s = ForwarderState()
        self.saves = 0

    def load(self):
        return self.s

    def save(self, st):
        self.s = st
        self.saves += 1


class FakeClaude:
    def __init__(self, events):
        self.events = events
        self.calls = []

    def fetch_window(self, start, end):
        self.calls.append((start, end))
        for raw in self.events:
            ev = ActivityEvent.from_payload(raw)
            if start <= ev.created_at_dt <= end:
                yield ev


class FakeHttp:
    def __init__(self, status, body=b""):
        self.status = status
        self.body = body

    def request(self, method, url, headers=None, body=None):
        class R:
            pass

        r = R()
        r.status = self.status
        r.data = self.body
        return r


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


# ── Test fixtures: Rev J Activity objects ──────────────────────────────────

NOW = datetime(2026, 5, 4, 12, 0, 0, tzinfo=timezone.utc)

# claude_chat_created event (Rev J example, page 24)
EV1 = {
    "id": "activity_1a2b3c4d5e",
    "created_at": _iso(NOW - timedelta(minutes=5)),
    "organization_id": "org_abc123",
    "organization_uuid": "abcdef0123-4567-89ab-cdef-0123456789ab",
    "actor": {
        "type": "user_actor",
        "email_address": "user@example.com",
        "user_id": "user_xyz456",
        "ip_address": "192.0.2.34",
        "user_agent": "Mozilla/5.0",
    },
    "type": "claude_chat_created",
    "claude_chat_id": "claude_chat_ijk012",
    "claude_project_id": None,
}

# api_key_created event with admin_api_key_actor
EV2 = {
    "id": "activity_2b3c4d5e6f",
    "created_at": _iso(NOW - timedelta(minutes=2)),
    "organization_id": "org_abc123",
    "organization_uuid": "abcdef0123-4567-89ab-cdef-0123456789ab",
    "actor": {
        "type": "admin_api_key_actor",
        "admin_api_key_id": "apikey_admin_abc123",
    },
    "type": "platform_api_key_created",
    "api_key_id": "apikey_xyz789",
}


# ── Tests ──────────────────────────────────────────────────────────────────


def test_s3_first_run():
    fake = FakeS3Client()
    egress = S3Egress("audit-bucket", prefix="claude-compliance", s3_client=fake)
    summary = run(FakeClaude([EV1, EV2]), egress, MemStore(), now=NOW)
    assert summary["forwarded"] == 2 and summary["skipped_duplicate"] == 0
    put = fake.calls[0]
    assert put["Bucket"] == "audit-bucket"
    assert put["ContentType"] == "application/x-ndjson"
    assert put["ContentEncoding"] == "gzip"
    assert put["ServerSideEncryption"] == "AES256"
    # S3 key uses wallclock at write time (matches CloudTrail/GuardDuty
    # partition convention), not the event timestamp.
    today = datetime.now(timezone.utc).strftime("%Y/%m/%d")
    assert put["Key"].startswith(f"claude-compliance/{today}/"), put["Key"]
    assert put["Key"].endswith(".jsonl.gz")
    lines = gzip.decompress(put["Body"]).decode().strip().split("\n")
    parsed = [json.loads(line) for line in lines]
    assert [e["type"] for e in parsed] == ["claude_chat_created", "platform_api_key_created"]
    assert [e["id"] for e in parsed] == ["activity_1a2b3c4d5e", "activity_2b3c4d5e6f"]
    print("OK test_s3_first_run")


def test_s3_dedupe_across_runs():
    store = MemStore()
    run(FakeClaude([EV1, EV2]), S3Egress("b", s3_client=FakeS3Client()), store, now=NOW)

    later = NOW + timedelta(minutes=5)
    ev3 = {
        "id": "activity_3c4d5e6f7g",
        "created_at": _iso(later - timedelta(seconds=30)),
        "organization_id": "org_abc123",
        "organization_uuid": "abcdef0123-4567-89ab-cdef-0123456789ab",
        "actor": {"type": "user_actor", "user_id": "user_xyz456"},
        "type": "claude_skill_created",
        "skill_id": "skill_abc",
        "skill_name": "python",
    }
    fake = FakeS3Client()
    s = run(FakeClaude([EV1, EV2, ev3]), S3Egress("b", s3_client=fake), store, now=later)
    assert s["forwarded"] == 1 and s["skipped_duplicate"] == 2
    lines = gzip.decompress(fake.calls[0]["Body"]).decode().strip().split("\n")
    assert json.loads(lines[0])["id"] == "activity_3c4d5e6f7g"
    assert json.loads(lines[0])["type"] == "claude_skill_created"
    print("OK test_s3_dedupe_across_runs")


def test_pubsub_attributes_match_rev_j_schema():
    pub = FakePublisher()
    egress = PubSubEgress("my-soc", "claude-audit", publisher=pub)
    n = egress.send([EV1, EV2])
    assert n == 2

    msg1 = pub.published[0]
    assert msg1["topic"] == "projects/my-soc/topics/claude-audit"
    assert msg1["attrs"]["activity_type"] == "claude_chat_created"
    assert msg1["attrs"]["actor_type"] == "user_actor"
    assert msg1["attrs"]["actor_user_id"] == "user_xyz456"
    assert msg1["attrs"]["organization_id"] == "org_abc123"

    msg2 = pub.published[1]
    assert msg2["attrs"]["activity_type"] == "platform_api_key_created"
    assert msg2["attrs"]["actor_type"] == "admin_api_key_actor"
    # admin_api_key_actor has no user_id; client_platform field doesn't exist
    # in Rev J at all — so neither attribute should be present.
    assert "actor_user_id" not in msg2["attrs"]
    assert "client_platform" not in msg2["attrs"]
    print("OK test_pubsub_attributes_match_rev_j_schema")


def test_pubsub_failure_propagates_and_keeps_watermark():
    store = MemStore()
    pub = FakePublisher(fail=True)
    egress = PubSubEgress("p", "t", publisher=pub)
    try:
        run(FakeClaude([EV1, EV2]), egress, store, now=NOW)
        raise AssertionError("expected RuntimeError")
    except RuntimeError as e:
        assert "publish failed" in str(e)
    assert store.saves == 0  # watermark must not advance on egress failure
    assert store.s.watermark is None
    print("OK test_pubsub_failure_propagates_and_keeps_watermark")


def test_admin_key_accepted():
    # Admin key (Console / API)
    ClaudeComplianceClient("sk-ant-admin01-test")
    print("OK test_admin_key_accepted")


def test_compliance_access_key_accepted():
    # Compliance Access Key (Claude.ai)
    ClaudeComplianceClient("sk-ant-api01-test")
    print("OK test_compliance_access_key_accepted")


def test_other_key_rejected():
    try:
        ClaudeComplianceClient("sk-some-other-key-prefix")
        raise AssertionError("expected ValueError")
    except ValueError as e:
        assert "Compliance API requires" in str(e)
        assert "sk-ant-admin01-" in str(e)
        assert "sk-ant-api01-" in str(e)
    print("OK test_other_key_rejected")


def test_404_error_message():
    c = ClaudeComplianceClient("sk-ant-admin01-test", http=FakeHttp(404, b"not found"))
    try:
        list(c.fetch_window(NOW - timedelta(minutes=10), NOW))
        raise AssertionError("expected ComplianceAPIError")
    except ComplianceAPIError as e:
        assert "/v1/compliance/activities" in str(e)
        assert "COMPLIANCE_API_PATH" in str(e)
    print("OK test_404_error_message")


def test_403_error_message():
    c = ClaudeComplianceClient("sk-ant-admin01-test", http=FakeHttp(403, b"forbidden"))
    try:
        list(c.fetch_window(NOW - timedelta(minutes=10), NOW))
        raise AssertionError("expected ComplianceAPIError")
    except ComplianceAPIError as e:
        assert "Compliance API is enabled" in str(e)
        assert "read:compliance_activities" in str(e)
    print("OK test_403_error_message")


def test_400_invalid_request_surfaces_message():
    body = (
        b'{"error":{"type":"invalid_request_error",'
        b'"message":"The created_at.gte parameter contains an invalid timestamp format."}}'
    )
    c = ClaudeComplianceClient("sk-ant-admin01-test", http=FakeHttp(400, body))
    try:
        list(c.fetch_window(NOW - timedelta(minutes=10), NOW))
        raise AssertionError("expected ComplianceAPIError")
    except ComplianceAPIError as e:
        assert "invalid timestamp" in str(e) or "created_at.gte" in str(e)
    print("OK test_400_invalid_request_surfaces_message")


def test_state_round_trip():
    s = ForwarderState(
        watermark="2026-05-04T12:00:00Z",
        recent_ids=["activity_a", "activity_b", "activity_c"],
    )
    restored = ForwarderState.from_dict(s.to_dict())
    assert restored.watermark == s.watermark
    assert restored.recent_ids == s.recent_ids
    print("OK test_state_round_trip")


def test_state_legacy_field_tolerated():
    # Older state docs (pre-Rev-J spec) used `recent_hashes`. New code reads
    # them transparently so an in-place upgrade doesn't lose dedupe history.
    legacy = {"watermark": "2026-05-04T12:00:00Z", "recent_hashes": ["h1", "h2"]}
    restored = ForwarderState.from_dict(legacy)
    assert restored.recent_ids == ["h1", "h2"]
    print("OK test_state_legacy_field_tolerated")


def test_id_set_bounded():
    big = set(f"activity_{i:08x}" for i in range(MAX_RECENT_IDS + 5000))
    result = _compute_state(big, "2026-05-04T12:00:00Z")
    assert len(result.recent_ids) == MAX_RECENT_IDS
    print("OK test_id_set_bounded")


def test_constants_match_rev_j():
    assert OVERLAP_SECONDS == 300
    assert MAX_RECENT_IDS == 10_000
    assert COMPLIANCE_API_PATH == "/v1/compliance/activities"
    print("OK test_constants_match_rev_j")


if __name__ == "__main__":
    test_s3_first_run()
    test_s3_dedupe_across_runs()
    test_pubsub_attributes_match_rev_j_schema()
    test_pubsub_failure_propagates_and_keeps_watermark()
    test_admin_key_accepted()
    test_compliance_access_key_accepted()
    test_other_key_rejected()
    test_404_error_message()
    test_403_error_message()
    test_400_invalid_request_surfaces_message()
    test_state_round_trip()
    test_state_legacy_field_tolerated()
    test_id_set_bounded()
    test_constants_match_rev_j()
    print("\nALL SMOKE TESTS PASSED")
