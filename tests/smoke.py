"""Smoke test for the forwarder core, S3 egress, and Pub/Sub egress.

Runs in CI without AWS or GCP credentials by injecting fake low-level
clients. Validates:
  - Module imports against real boto3 / google-cloud-pubsub SDKs
  - First-run + subsequent-run watermark + content-hash dedupe
  - S3 object format (gzipped JSON-lines, AES256, content type)
  - Pub/Sub message format (per-event, attributes for routing)
  - Pub/Sub publish failure aborts the run before watermark advance
  - Compliance API client error handling (404, 403, admin-key validation)
  - State bound at MAX_RECENT_HASHES
"""

from __future__ import annotations

import gzip
import json
from datetime import datetime, timedelta, timezone

from forwarder.aws_handler import handler as aws_handler  # noqa: F401
from forwarder.claude_client import (
    AuditEvent,
    COMPLIANCE_API_PATH,
    ClaudeComplianceClient,
    ComplianceAPIError,
)
from forwarder.core import MAX_RECENT_HASHES, OVERLAP_SECONDS, _compute_state, run
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
            ev = AuditEvent.from_payload(raw)
            if start <= ev.created_at_dt < end:
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


# ── Test fixtures ──────────────────────────────────────────────────────────

NOW = datetime(2026, 5, 4, 12, 0, 0, tzinfo=timezone.utc)
EV1 = {
    "created_at": _iso(NOW - timedelta(minutes=5)),
    "event": "user.signin",
    "actor_info": {"user_id": "u1"},
    "event_info": {},
    "entity_info": {},
    "ip_address": "203.0.113.10",
    "user_agent": "Mozilla/5.0",
    "client_platform": "web",
    "device_id": "d-1",
}
EV2 = {
    "created_at": _iso(NOW - timedelta(minutes=2)),
    "event": "apikey.create",
    "actor_info": {"user_id": "u1", "email": "alice@example.com"},
    "event_info": {"key_id": "k_abc"},
    "entity_info": {"workspace_id": "wrkspc_1"},
    "ip_address": "203.0.113.10",
    "user_agent": "Mozilla/5.0",
    "client_platform": "web",
    "device_id": "d-1",
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
    assert put["Key"].startswith("claude-compliance/2026/05/04/")
    assert put["Key"].endswith(".jsonl.gz")
    lines = gzip.decompress(put["Body"]).decode().strip().split("\n")
    assert [json.loads(l)["event"] for l in lines] == ["user.signin", "apikey.create"]
    print("OK test_s3_first_run")


def test_s3_dedupe_across_runs():
    store = MemStore()
    run(FakeClaude([EV1, EV2]), S3Egress("b", s3_client=FakeS3Client()), store, now=NOW)

    later = NOW + timedelta(minutes=5)
    ev3 = dict(EV1, created_at=_iso(later - timedelta(seconds=30)), event="skill.create")
    fake = FakeS3Client()
    s = run(FakeClaude([EV1, EV2, ev3]), S3Egress("b", s3_client=fake), store, now=later)
    assert s["forwarded"] == 1 and s["skipped_duplicate"] == 2
    lines = gzip.decompress(fake.calls[0]["Body"]).decode().strip().split("\n")
    assert json.loads(lines[0])["event"] == "skill.create"
    print("OK test_s3_dedupe_across_runs")


def test_overlap_window_applied():
    store = MemStore()
    run(FakeClaude([EV1, EV2]), S3Egress("b", s3_client=FakeS3Client()), store, now=NOW)
    later = NOW + timedelta(minutes=5)
    fc = FakeClaude([EV2])
    run(fc, S3Egress("b", s3_client=FakeS3Client()), store, now=later)
    start, _ = fc.calls[0]
    expected = datetime.fromisoformat(store.s.watermark.replace("Z", "+00:00")) - timedelta(seconds=OVERLAP_SECONDS)
    # store.s.watermark is now EV2's created_at; the *next* run would query starting OVERLAP before that
    assert start <= expected + timedelta(seconds=1)
    print("OK test_overlap_window_applied")


def test_pubsub_attributes_and_format():
    pub = FakePublisher()
    egress = PubSubEgress("my-soc", "claude-audit", publisher=pub)
    n = egress.send([EV1, EV2])
    assert n == 2
    assert all(p["topic"] == "projects/my-soc/topics/claude-audit" for p in pub.published)
    assert pub.published[0]["attrs"]["event"] == "user.signin"
    assert pub.published[0]["attrs"]["actor_user_id"] == "u1"
    assert pub.published[0]["attrs"]["client_platform"] == "web"
    print("OK test_pubsub_attributes_and_format")


def test_pubsub_failure_propagates():
    pub = FakePublisher(fail=True)
    egress = PubSubEgress("p", "t", publisher=pub)
    try:
        egress.send([EV1])
        raise AssertionError("expected RuntimeError")
    except RuntimeError as e:
        assert "publish failed" in str(e)
    print("OK test_pubsub_failure_propagates")


def test_pubsub_failure_keeps_watermark():
    store = MemStore()
    pub = FakePublisher(fail=True)
    egress = PubSubEgress("p", "t", publisher=pub)
    try:
        run(FakeClaude([EV1, EV2]), egress, store, now=NOW)
        raise AssertionError("expected RuntimeError")
    except RuntimeError:
        pass
    assert store.saves == 0  # watermark must not advance on failure
    assert store.s.watermark is None
    print("OK test_pubsub_failure_keeps_watermark")


def test_admin_key_validation():
    try:
        ClaudeComplianceClient("sk-ant-api01-not-an-admin-key")
        raise AssertionError("expected ValueError")
    except ValueError as e:
        assert "Admin API key" in str(e)
    print("OK test_admin_key_validation")


def test_404_error_message():
    c = ClaudeComplianceClient("sk-ant-admin01-test", http=FakeHttp(404, b"not found"))
    try:
        list(c.fetch_window(NOW - timedelta(minutes=10), NOW))
        raise AssertionError("expected ComplianceAPIError")
    except ComplianceAPIError as e:
        assert "COMPLIANCE_API_PATH" in str(e) and "Trust Center" in str(e)
    print("OK test_404_error_message")


def test_403_error_message():
    c = ClaudeComplianceClient("sk-ant-admin01-test", http=FakeHttp(403, b"forbidden"))
    try:
        list(c.fetch_window(NOW - timedelta(minutes=10), NOW))
        raise AssertionError("expected ComplianceAPIError")
    except ComplianceAPIError as e:
        assert "Compliance API is enabled" in str(e)
    print("OK test_403_error_message")


def test_state_round_trip():
    s = ForwarderState(watermark="2026-05-04T12:00:00Z", recent_hashes=["a", "b", "c"])
    restored = ForwarderState.from_dict(s.to_dict())
    assert restored.watermark == s.watermark
    assert restored.recent_hashes == s.recent_hashes
    print("OK test_state_round_trip")


def test_hash_set_bounded():
    big = set(f"{i:064x}" for i in range(MAX_RECENT_HASHES + 5000))
    result = _compute_state(big, "2026-05-04T12:00:00Z")
    assert len(result.recent_hashes) == MAX_RECENT_HASHES
    print("OK test_hash_set_bounded")


def test_constants_sanity():
    assert OVERLAP_SECONDS == 300
    assert MAX_RECENT_HASHES == 10_000
    assert COMPLIANCE_API_PATH.startswith("/v1/")
    print("OK test_constants_sanity")


if __name__ == "__main__":
    test_s3_first_run()
    test_s3_dedupe_across_runs()
    test_overlap_window_applied()
    test_pubsub_attributes_and_format()
    test_pubsub_failure_propagates()
    test_pubsub_failure_keeps_watermark()
    test_admin_key_validation()
    test_404_error_message()
    test_403_error_message()
    test_state_round_trip()
    test_hash_set_bounded()
    test_constants_sanity()
    print("\nALL SMOKE TESTS PASSED")
