"""Online Discord capability preflight using catalog identities and channels."""

from __future__ import annotations

from typing import Any, Mapping


def run_preflight(catalog: Mapping[str, Any], clients: Mapping[str, Any]) -> dict[str, Any]:
    agents = catalog.get("agents") if isinstance(catalog, Mapping) else None
    if not isinstance(agents, Mapping):
        return {"ready": False, "errors": ["catalog has no agents"]}
    leaders = [(key, value) for key, value in agents.items() if isinstance(value, Mapping) and value.get("role") == "leader"]
    workers = [(key, value) for key, value in agents.items() if isinstance(value, Mapping) and value.get("role") != "leader"]
    if len(leaders) != 1:
        return {"ready": False, "errors": ["catalog must contain exactly one leader"]}
    leader_id, leader = leaders[0]
    result: dict[str, Any] = {"ready": True, "leader": leader_id, "workers": {}, "errors": []}
    leader_discord = leader.get("discord", {})
    leader_trusted = set(leader_discord.get("trusted_bot_ids", [])) if isinstance(leader_discord, Mapping) else set()
    worker_bots = {value.get("discord", {}).get("bot_user_id") for _, value in workers if isinstance(value.get("discord"), Mapping)}
    if leader_trusted != worker_bots:
        result["errors"].append("leader trusted bot IDs do not equal the worker bot IDs")
    for worker_id, worker in workers:
        discord = worker.get("discord", {})
        channel = discord.get("work_channel_id") if isinstance(discord, Mapping) else None
        bot_id = discord.get("bot_user_id") if isinstance(discord, Mapping) else None
        # A thread does not exist until the dispatch message is accepted by
        # Discord.  Keep this capability explicitly deferred; claiming it was
        # preflighted would make a green result stronger than the evidence.
        item: dict[str, Any] = {"channel_id": channel, "target_bot_user_id": bot_id, "worker_read": False, "leader_read": False, "leader_send": False, "thread_access": "deferred_to_dispatch"}
        try:
            client = clients[worker_id]
            client.get_channel(channel)
            client.read_messages(channel)
            item["worker_read"] = True
        except Exception as exc:
            item["error"] = _error("worker channel preflight failed", exc)
        try:
            leader_client = clients[leader_id]
            leader_client.get_channel(channel)
            leader_client.read_messages(channel)
            leader_client.trigger_typing(channel)
            item["leader_read"] = True
            item["leader_send"] = True
        except Exception as exc:
            item["error"] = _error("leader channel preflight failed", exc)
        trusted = discord.get("trusted_bot_ids") if isinstance(discord, Mapping) else None
        if trusted != [leader_discord.get("bot_user_id")]:
            item["error"] = "worker trusted bot IDs do not contain exactly the leader bot"
        result["workers"][worker_id] = item
        if not item.get("worker_read") or not item.get("leader_read") or not item.get("leader_send") or item.get("error"):
            result["ready"] = False
    if result["errors"]:
        result["ready"] = False
    return result


def _error(prefix: str, exc: Exception) -> str:
    status = getattr(exc, "code", None)
    suffix = f" HTTP {status}" if isinstance(status, int) else f" {type(exc).__name__}"
    return prefix + ":" + suffix
