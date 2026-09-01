"""Render normalized catalog profiles into the upstream OpenAB Helm shape."""

from __future__ import annotations

import json
from typing import Any, Mapping

import tomllib
import yaml

from .k8s import agent_service_account_name


class RenderError(ValueError):
    """Raised when a catalog cannot be mapped to supported OpenAB values."""


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _toml_array(values: list[str]) -> str:
    return "[" + ", ".join(_toml_string(value) for value in values) + "]"


def render_config_toml(agent_id: str, agent: Mapping[str, Any], *, working_dir: str = "/home/agent") -> str:
    """Render only OpenAB-supported fields and prove the result parses as TOML."""

    discord = agent["discord"]
    channels = [channel for channel in (discord.get("entry_channel_id"), discord.get("work_channel_id")) if channel]
    if len(channels) != 1:
        raise RenderError(f"{agent_id}: exactly one Discord channel is required")
    runtime = agent["runtime"]
    lines = [
        "[discord]",
        'bot_token = "${DISCORD_BOT_TOKEN}"',
        f"allowed_channels = {_toml_array(channels)}",
        f"allowed_users = {_toml_array(discord['allowed_users'])}",
        f"allow_all_channels = {str(discord['allow_all_channels']).lower()}",
        f"allow_all_users = {str(discord['allow_all_users']).lower()}",
        f"allow_bot_messages = {_toml_string(discord['allow_bot_messages'])}",
        f"trusted_bot_ids = {_toml_array(discord['trusted_bot_ids'])}",
        f"allow_user_messages = {_toml_string(discord['allow_user_messages'])}",
        "",
        "[agent]",
        f"command = {_toml_string(runtime['command'])}",
        f"args = {_toml_array(runtime.get('args', []))}",
        f"working_dir = {_toml_string(working_dir)}",
        "",
        "[pool]",
        f"default_config_options = {{ model = {_toml_string(runtime['model'])} }}",
    ]
    rendered = "\n".join(lines) + "\n"
    try:
        parsed = tomllib.loads(rendered)
    except tomllib.TOMLDecodeError as exc:
        raise RenderError(f"{agent_id}: generated config.toml is invalid") from exc
    if parsed.get("pool", {}).get("default_config_options", {}).get("model") != runtime["model"]:
        raise RenderError(f"{agent_id}: model did not map to pool.default_config_options.model")
    return rendered


def render_openab_values(catalog: Mapping[str, Any], *, runtime_volume_size: str = "10Gi") -> dict[str, Any]:
    """Return values consumable by ``charts/openab`` without Secret values."""

    agents: dict[str, Any] = {}
    for agent_id, agent in sorted(catalog["agents"].items()):
        secret_name, secret_key = agent["discord"]["bot_secret_ref"].split("/", 1)
        mounts: list[dict[str, Any]] = []
        volumes: list[dict[str, Any]] = []
        worktree = agent["worktree"]
        for index, grant in enumerate(agent["repository_grants"]):
            name = f"repo-{index}"
            subpath = grant["checkout_subpath"]
            mounts.append({
                "name": name,
                "mountPath": f"{worktree['container_mount_path']}/{subpath}",
                "readOnly": grant["access"] == "read",
            })
            volumes.append({
                "name": name,
                "hostPath": {"path": f"{worktree['path']}/{subpath}", "type": "Directory"},
            })
        agents[agent_id] = {
            "enabled": True,
            "nameOverride": agent_id,
            "serviceAccountName": agent_service_account_name(agent_id),
            "image": agent["runtime"]["image"],
            "workingDir": "/home/agent",
            "persistence": {"enabled": True, "size": runtime_volume_size},
            "secretEnv": [{"name": "DISCORD_BOT_TOKEN", "secretName": secret_name, "secretKey": secret_key}],
            "configToml": render_config_toml(agent_id, agent),
            "resources": agent["resources"],
            "extraVolumeMounts": mounts,
            "extraVolumes": volumes,
        }
    # The upstream chart ships a sample ``agents.kiro`` entry enabled by
    # default.  Helm deep-merges values, so omitting that key would leave an
    # unexpected fifth workload (and, on chart v0.10+, fail because its
    # sample configToml is empty).  Explicitly disable the sample entry while
    # keeping the catalog as the sole source of enabled agents.
    agents_with_defaults = {"kiro": {"enabled": False}}
    agents_with_defaults.update(agents)
    return {
        "image": {"repository": "ghcr.io/openabdev/openab", "pullPolicy": "IfNotPresent"},
        # Pin the chart-level safeguards rather than relying on upstream
        # defaults.  Agent checkouts and /tmp are the only intended writable
        # mounts; no workload receives a privileged host context.
        "podSecurityContext": {
            "runAsNonRoot": True,
            "runAsUser": 1000,
            "runAsGroup": 1000,
            "fsGroup": 1000,
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "containerSecurityContext": {
            "allowPrivilegeEscalation": False,
            "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"]},
        },
        "agents": agents_with_defaults,
    }


def render_openab_values_yaml(catalog: Mapping[str, Any], *, runtime_volume_size: str = "10Gi") -> str:
    """Serialize renderer output as a stable, non-secret Helm values document."""

    values = render_openab_values(catalog, runtime_volume_size=runtime_volume_size)
    return yaml.safe_dump(values, allow_unicode=True, sort_keys=False, default_flow_style=False)
