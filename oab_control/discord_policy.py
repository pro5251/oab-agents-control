"""Pure Discord admission and dispatch policy for the single-leader topology.

The module deliberately does not import a Discord SDK or perform network I/O.
It is the policy seam used by a future Discord adapter and by local/script
callers: the adapter supplies a normalized catalog and a message envelope,
then applies the returned decision and persists the audit event.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


class DiscordPolicyError(ValueError):
    """Raised when a policy input cannot be evaluated safely."""


@dataclass(frozen=True)
class DiscordMessage:
    """Minimal provider-neutral message facts needed for admission."""

    channel_id: str
    author_id: str
    author_is_bot: bool
    mentioned_bot_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not all(isinstance(value, str) and value for value in (self.channel_id, self.author_id)):
            raise DiscordPolicyError("message channel and author IDs are required")
        if not isinstance(self.author_is_bot, bool):
            raise DiscordPolicyError("message author_is_bot must be a boolean")
        if not isinstance(self.mentioned_bot_ids, tuple) or not all(isinstance(value, str) and value for value in self.mentioned_bot_ids):
            raise DiscordPolicyError("message mentions must be a tuple of IDs")


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reason: str
    audit_event: dict[str, Any]


def evaluate_message(
    catalog: Mapping[str, Any],
    *,
    agent_id: str,
    message: DiscordMessage,
) -> PolicyDecision:
    """Evaluate one inbound message against one normalized catalog agent.

    No message content is accepted or copied, so the resulting audit event is
    safe to persist.  A failed decision is still an audit event: adapters must
    not silently drop denied traffic.
    """

    agent = _agent(catalog, agent_id)
    discord = agent.get("discord")
    if not isinstance(discord, Mapping):
        raise DiscordPolicyError("catalog agent has no Discord policy")
    bot_id = _required_text(discord, "bot_user_id")
    channel = _channel(discord, agent.get("role"))
    audit = {
        "event": "discord_message_admission",
        "agent_id": agent_id,
        "channel_id": message.channel_id,
        "author_id": message.author_id,
        "author_is_bot": message.author_is_bot,
        "allowed": False,
    }

    if discord.get("allow_all_channels") is not False or discord.get("allow_all_users") is not False:
        return _deny(audit, "catalog policy must explicitly deny all channels and users")
    if message.channel_id != channel:
        return _deny(audit, "message channel is not the agent's configured private/entry channel")

    mentions = set(message.mentioned_bot_ids)
    if message.author_is_bot:
        trusted = discord.get("trusted_bot_ids")
        if not isinstance(trusted, list) or message.author_id not in trusted:
            return _deny(audit, "bot author is not trusted by this agent")
        if discord.get("allow_bot_messages") != "mentions":
            return _deny(audit, "bot messages require the mentions policy")
        if bot_id not in mentions:
            return _deny(audit, "trusted bot message does not mention the receiving agent")
        return _allow(audit, "trusted bot mention accepted")

    allowed_users = discord.get("allowed_users")
    if not isinstance(allowed_users, list) or message.author_id not in allowed_users:
        return _deny(audit, "human author is not in the explicit allowlist")
    if agent.get("role") != "leader":
        return _deny(audit, "worker channels reject human messages")
    if discord.get("allow_user_messages") != "multibot-mentions":
        return _deny(audit, "human messages require the multibot-mentions policy")
    if bot_id not in mentions:
        return _deny(audit, "human message does not mention the leader bot")
    return _allow(audit, "allowed leader human mention accepted")


def dispatch_channel(catalog: Mapping[str, Any], *, agent_id: str, task_envelope: Mapping[str, Any]) -> str:
    """Resolve a leader-to-worker dispatch target from a task envelope.

    Only the leader may call this operation in the surrounding Control CLI;
    this pure function additionally rejects malformed or cross-agent routing.
    """

    agent = _agent(catalog, agent_id)
    if agent.get("role") == "leader":
        raise DiscordPolicyError("dispatch target must be a worker agent")
    required = ("task_id", "repo", "checkout_subpath", "worktree_path", "branch", "base_branch", "delivery_owner", "deadline", "reply_to")
    if any(not isinstance(task_envelope.get(key), str) or not task_envelope[key].strip() for key in required):
        raise DiscordPolicyError("task envelope is missing required routing fields")
    if task_envelope.get("agent_id") not in {None, agent_id}:
        raise DiscordPolicyError("task envelope belongs to a different agent")
    work_channel = _channel(agent.get("discord", {}), agent.get("role"))
    if not work_channel:
        raise DiscordPolicyError("worker has no private work channel")
    return work_channel


def _agent(catalog: Mapping[str, Any], agent_id: str) -> Mapping[str, Any]:
    agents = catalog.get("agents") if isinstance(catalog, Mapping) else None
    if not isinstance(agents, Mapping) or not isinstance(agent_id, str) or not isinstance(agents.get(agent_id), Mapping):
        raise DiscordPolicyError("unknown catalog agent")
    return agents[agent_id]


def _channel(discord: Mapping[str, Any], role: Any) -> str:
    key = "entry_channel_id" if role == "leader" else "work_channel_id"
    value = discord.get(key)
    if not isinstance(value, str) or not value:
        raise DiscordPolicyError("agent has no role-appropriate Discord channel")
    return value


def _required_text(value: Mapping[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise DiscordPolicyError(f"Discord policy field is missing: {key}")
    return item


def _allow(audit: dict[str, Any], reason: str) -> PolicyDecision:
    payload = {**audit, "allowed": True, "reason": reason}
    return PolicyDecision(True, reason, payload)


def _deny(audit: dict[str, Any], reason: str) -> PolicyDecision:
    payload = {**audit, "reason": reason}
    return PolicyDecision(False, reason, payload)
