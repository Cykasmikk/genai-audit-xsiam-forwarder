"""Firestore-backed forwarder state store."""

from __future__ import annotations

from google.cloud import firestore

from .state import ForwarderState

COLLECTION = "claude_compliance_forwarder"
DOC_ID = "state"


class FirestoreStateStore:
    def __init__(self, project: str | None = None):
        self._client = firestore.Client(project=project)
        self._doc = self._client.collection(COLLECTION).document(DOC_ID)

    def load(self) -> ForwarderState:
        snap = self._doc.get()
        return ForwarderState.from_dict(snap.to_dict() if snap.exists else None)

    def save(self, state: ForwarderState) -> None:
        self._doc.set(state.to_dict())
