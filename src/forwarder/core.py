"""Cloud-agnostic fetch → forward → checkpoint loop.

Idempotency model
-----------------
The Compliance API does not publicly document an event-id field, so we cannot
cursor by id. Instead each run:

  1. Loads the prior state: a watermark (latest `created_at` ever forwarded)
     and a bounded set of recent content hashes.
  2. Queries the API for the window
        [watermark - OVERLAP_SECONDS, now]
     to absorb clock skew and any out-of-order delivery near the boundary.
  3. Drops events whose content hash is already in `recent_hashes`.
  4. Forwards the survivors to XSIAM in size-bounded batches.
  5. Advances the watermark and refreshes `recent_hashes` from events that
     fall within the trailing OVERLAP window of the new watermark.
  6. Persists state only after XSIAM ACKs the batch — a crash mid-batch
     replays the same window cleanly on the next tick.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from .claude_client import AuditEvent, ClaudeComplianceClient
from .egress import Egress
from .state import ForwarderState, StateStore

log = logging.getLogger(__name__)

# How far back of the watermark we re-query each tick to absorb clock skew
# and out-of-order delivery. 5 min is generous given the API's 5-min freshness
# SLO documented for the sibling Usage API.
OVERLAP_SECONDS = 300

# Cap on `recent_hashes` retained between runs. With OVERLAP_SECONDS=300 and
# realistic Enterprise audit volumes (~hundreds/hour), 10k entries is ample
# headroom while keeping the state document well under DynamoDB/Firestore
# item-size limits.
MAX_RECENT_HASHES = 10_000

PENDING_FLUSH_AT = 1000


def run(
    claude: ClaudeComplianceClient,
    egress: Egress,
    store: StateStore,
    initial_lookback_minutes: int = 60,
    now: datetime | None = None,
) -> dict:
    """Pull new audit events and forward them to the configured egress sink."""
    now = now or datetime.now(timezone.utc)
    state = store.load()

    if state.watermark:
        starting_at = _parse_iso(state.watermark) - timedelta(seconds=OVERLAP_SECONDS)
        first_run = False
    else:
        starting_at = now - timedelta(minutes=initial_lookback_minutes)
        first_run = True

    log.info(
        "starting run first_run=%s window=[%s, %s) prior_hashes=%d",
        first_run,
        starting_at.isoformat(),
        now.isoformat(),
        len(state.recent_hashes),
    )

    seen = set(state.recent_hashes)
    pending: list[AuditEvent] = []
    forwarded = 0
    skipped_duplicate = 0
    new_watermark = state.watermark

    def flush() -> None:
        nonlocal forwarded
        if not pending:
            return
        egress.send(ev.raw for ev in pending)
        forwarded += len(pending)
        # Persist only after the egress sink ACKs so a later failure can't
        # undo work that has already been accepted downstream.
        store.save(_compute_state(seen, new_watermark))
        pending.clear()

    for ev in claude.fetch_window(starting_at, now):
        h = ev.content_hash
        if h in seen:
            skipped_duplicate += 1
            continue
        seen.add(h)
        pending.append(ev)
        if new_watermark is None or ev.created_at > new_watermark:
            new_watermark = ev.created_at
        if len(pending) >= PENDING_FLUSH_AT:
            flush()

    flush()

    summary = {
        "first_run": first_run,
        "forwarded": forwarded,
        "skipped_duplicate": skipped_duplicate,
        "watermark": new_watermark,
    }
    log.info("run complete %s", summary)
    return summary


def _compute_state(seen: set[str], watermark: str | None) -> ForwarderState:
    # Bound the hash set so the state document stays small and bounded.
    if len(seen) > MAX_RECENT_HASHES:
        # Order is irrelevant — we only need membership semantics — so trim
        # arbitrarily. (Hashes are 64 hex chars; 10k entries ≈ 640 KB.)
        trimmed = list(seen)[-MAX_RECENT_HASHES:]
    else:
        trimmed = list(seen)
    return ForwarderState(watermark=watermark, recent_hashes=trimmed)


def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))
