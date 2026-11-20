"""
NotificationDispatcher — routes AgentDB governance events to notification channels.

Usage::

    from agentdb import AgentDB, EmailChannel, NotificationDispatcher

    db = AgentDB("memory.db")
    channel = EmailChannel("ops@example.com", smtp_host="smtp.example.com", smtp_port=587,
                           smtp_user="user", smtp_password="secret", use_tls=True)
    dispatcher = NotificationDispatcher(db.gateway, channels=[channel])
    dispatcher.attach()

The dispatcher subscribes to gateway events and calls ``channel.send(subject, body)``
for each one.  Multiple channels receive every event.

Default subscribed events:
  - skill_stale
  - skill_review_needed
  - untracked_skill_candidate
  - conflict_detected
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from .channels import NotificationChannel

if TYPE_CHECKING:
    from ..gateway import Gateway

_DEFAULT_EVENTS = (
    "skill_stale",
    "skill_review_needed",
    "untracked_skill_candidate",
    "conflict_detected",
)


def _format(event: str, kwargs: dict) -> tuple[str, str]:
    """Return (subject, body) for a governance event."""
    if event == "skill_stale":
        skill_id = kwargs.get("skill_id", "unknown")
        return (
            f"AgentDB: Skill '{skill_id}' is stale",
            (
                f"Skill '{skill_id}' has crossed the staleness threshold (health < 0.6).\n"
                "Review or update its content so agents receive current guidance."
            ),
        )

    if event == "skill_review_needed":
        skill_id = kwargs.get("skill_id", "unknown")
        memory_key = kwargs.get("memory_key", "unknown")
        agent_id = kwargs.get("agent_id", "unknown")
        return (
            f"AgentDB: Skill conflict signal for '{skill_id}'",
            (
                f"Agent '{agent_id}' wrote memory '{memory_key}' whose keywords overlap "
                f"skill '{skill_id}'.\n"
                "Review whether the memory contradicts, extends, or duplicates the skill."
            ),
        )

    if event == "untracked_skill_candidate":
        sample_key = kwargs.get("sample_key", "unknown")
        cluster_size = kwargs.get("cluster_size", "?")
        return (
            "AgentDB: Untracked skill candidate detected",
            (
                f"Memory consolidation found a cluster of {cluster_size} records "
                f"(sample key: '{sample_key}') with no governing skill.\n"
                "Consider creating a skill to govern this knowledge domain."
            ),
        )

    if event == "conflict_detected":
        key = kwargs.get("key", "unknown")
        agent_id = kwargs.get("agent_id", "unknown")
        conflict_type = kwargs.get("conflict_type", "unknown")
        return (
            f"AgentDB: Memory conflict detected for '{key}'",
            (
                f"A {conflict_type} conflict was detected for memory key '{key}' "
                f"written by agent '{agent_id}'.\n"
                "Check the conflict queue: db.gateway.conflict_queue()"
            ),
        )

    # Generic fallback for custom events
    return (
        f"AgentDB: {event}",
        "\n".join(f"{k}: {v}" for k, v in kwargs.items()) or "(no details)",
    )


class NotificationDispatcher:
    """
    Routes AgentDB governance events to one or more notification channels.

    Args:
        gateway: The ``db.gateway`` instance to subscribe to.
        channels: One or more :class:`NotificationChannel` instances.
        events: Event names to subscribe to.  Defaults to the four standard
            governance events.  Pass an empty list to subscribe to nothing.
    """

    def __init__(
        self,
        gateway: "Gateway",
        channels: list[NotificationChannel],
        events: Optional[list[str]] = None,
    ) -> None:
        self._gateway = gateway
        self._channels = list(channels)
        self._events = list(events) if events is not None else list(_DEFAULT_EVENTS)

    def attach(self) -> None:
        """Subscribe to all configured events on the gateway."""
        for event in self._events:
            self._gateway.subscribe(event, self._make_handler(event))

    def _make_handler(self, event: str):
        def _handler(**kwargs):
            subject, body = _format(event, kwargs)
            for channel in self._channels:
                try:
                    channel.send(subject, body)
                except Exception as exc:  # noqa: BLE001
                    # Log and continue — one failing channel must not block others.
                    import logging
                    logging.getLogger(__name__).warning(
                        "Notification channel %r failed for event %r: %s",
                        type(channel).__name__,
                        event,
                        exc,
                    )
        return _handler
