"""Pub/Sub egress: publishes audit events to a Pub/Sub topic.

XSIAM's "GCP Pub/Sub" data source pulls from a customer-owned subscription
using a service account credentials file. We publish; XSIAM consumes.

Each event becomes one Pub/Sub message:
- `data` is the raw vendor-native event JSON (UTF-8 bytes).
- `attributes` carry small routing/filter hints — most importantly `vendor`
  so a single Pub/Sub topic can carry multiple vendors and XSIAM-side
  filtering or per-vendor subscriptions can split them out. Per-vendor
  attributes (activity_type, actor_*) are extracted with vendor-aware logic
  so OpenAI's nested actor schema and Anthropic's flat-with-discriminator
  schema both produce useful filter keys.

We block on each publish future to surface failures synchronously, so the
forwarder can refuse to advance the watermark past unsent events.
"""

from __future__ import annotations

import json
import logging
from typing import Iterable

log = logging.getLogger(__name__)


class PubSubEgress:
    def __init__(self, project: str, topic: str, vendor: str, publisher=None):
        self._project = project
        self._topic = topic
        self._vendor = vendor
        if publisher is not None:
            self._publisher = publisher
        else:
            from google.cloud import pubsub_v1  # deferred for dev import

            self._publisher = pubsub_v1.PublisherClient(
                publisher_options=pubsub_v1.types.PublisherOptions(
                    enable_message_ordering=False,
                )
            )
        self._topic_path = self._publisher.topic_path(project, topic)

    def send(self, events: Iterable[dict]) -> int:
        materialized = list(events)
        if not materialized:
            return 0

        futures = []
        for ev in materialized:
            data = json.dumps(ev, separators=(",", ":")).encode("utf-8")
            attrs = self._attributes(ev)
            futures.append(self._publisher.publish(self._topic_path, data, **attrs))

        for fut in futures:
            fut.result(timeout=30)

        log.info(
            "%s: published %d events to projects/%s/topics/%s",
            self._vendor,
            len(materialized),
            self._project,
            self._topic,
        )
        return len(materialized)

    def _attributes(self, ev: dict) -> dict:
        # Pub/Sub attribute values have a 1024-byte cap; keep them tiny.
        attrs: dict = {"vendor": self._vendor}
        if self._vendor == "anthropic":
            self._anthropic_attrs(ev, attrs)
        elif self._vendor == "anthropic_chats":
            self._anthropic_chats_attrs(ev, attrs)
        elif self._vendor == "openai":
            self._openai_attrs(ev, attrs)
        elif self._vendor == "openai_conversations":
            self._openai_conversations_attrs(ev, attrs)
        return attrs

    @staticmethod
    def _anthropic_attrs(ev: dict, attrs: dict) -> None:
        # Compliance API Rev J Activity object: top-level `type`,
        # `organization_id`; nested `actor.{type, user_id, api_key_id}`.
        if isinstance(ev.get("type"), str):
            attrs["activity_type"] = ev["type"][:256]
        if isinstance(ev.get("organization_id"), str):
            attrs["organization_id"] = ev["organization_id"][:64]
        actor = ev.get("actor") or {}
        if isinstance(actor.get("type"), str):
            attrs["actor_type"] = actor["type"][:64]
        if isinstance(actor.get("user_id"), str):
            attrs["actor_user_id"] = actor["user_id"][:256]
        elif isinstance(actor.get("api_key_id"), str):
            attrs["actor_api_key_id"] = actor["api_key_id"][:256]

    @staticmethod
    def _anthropic_chats_attrs(ev: dict, attrs: dict) -> None:
        # Wrapped payload from anthropic_chat_content adapter:
        #   {"chat": {chat metadata}, "message": {message body}}
        chat = ev.get("chat") or {}
        message = ev.get("message") or {}
        if isinstance(chat.get("id"), str):
            attrs["chat_id"] = chat["id"][:128]
        if isinstance(chat.get("organization_id"), str):
            attrs["organization_id"] = chat["organization_id"][:64]
        if isinstance(chat.get("project_id"), str):
            attrs["project_id"] = chat["project_id"][:128]
        user = chat.get("user") or {}
        if isinstance(user.get("id"), str):
            attrs["actor_user_id"] = user["id"][:256]
        if isinstance(message.get("role"), str):
            attrs["message_role"] = message["role"][:32]

    @staticmethod
    def _openai_attrs(ev: dict, attrs: dict) -> None:
        # OpenAI Audit Logs object: top-level `type`, `project.id`; nested
        # actor as either actor.session.user.{id, email} or
        # actor.api_key.{id, type, user.id}.
        if isinstance(ev.get("type"), str):
            attrs["activity_type"] = ev["type"][:256]
        project = ev.get("project") or {}
        if isinstance(project.get("id"), str):
            attrs["project_id"] = project["id"][:64]
        actor = ev.get("actor") or {}
        if "session" in actor and isinstance(actor["session"], dict):
            attrs["actor_type"] = "session"
            user = actor["session"].get("user") or {}
            if isinstance(user.get("id"), str):
                attrs["actor_user_id"] = user["id"][:256]
        elif "api_key" in actor and isinstance(actor["api_key"], dict):
            attrs["actor_type"] = "api_key"
            ak = actor["api_key"]
            if isinstance(ak.get("id"), str):
                attrs["actor_api_key_id"] = ak["id"][:256]

    @staticmethod
    def _openai_conversations_attrs(ev: dict, attrs: dict) -> None:
        # Wrapped payload from openai_conversations adapter:
        #   {"conversation": {convo meta}, "message": {message body}}
        # OR the message-level shape directly if the spec returns flat records.
        convo = ev.get("conversation") or {}
        message = ev.get("message") if "message" in ev else ev
        if isinstance(convo.get("id"), str):
            attrs["conversation_id"] = convo["id"][:128]
        if isinstance(convo.get("workspace_id"), str):
            attrs["workspace_id"] = convo["workspace_id"][:64]
        if isinstance(message.get("role"), str):
            attrs["message_role"] = message["role"][:32]
        if isinstance(message.get("model"), str):
            attrs["model"] = message["model"][:64]
        # Fallback: actor_user_id at the message level
        if isinstance(message.get("user_id"), str):
            attrs["actor_user_id"] = message["user_id"][:256]
        elif isinstance(convo.get("user_id"), str):
            attrs["actor_user_id"] = convo["user_id"][:256]
