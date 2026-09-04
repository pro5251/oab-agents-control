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


def render_config_toml(agent_id: str, agent: Mapping[str, Any], *, working_dir: str | None = None) -> str:
    """Render only OpenAB-supported fields and prove the result parses as TOML.

    ``working_dir`` defaults to the agent's own ``runtime.working_dir`` so that
    an image whose user home is not ``/home/agent`` still receives a writable
    home; an explicit argument overrides it.
    """

    discord = agent["discord"]
    working_dir = working_dir or agent["runtime"].get("working_dir", "/home/agent")
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


def render_agents_md(agent_id: str, agent: Mapping[str, Any]) -> str:
    """Tell the agent where its workspaces are.

    Mounting a checkout is not the same as the agent knowing about it.  The
    process starts in its home directory, and nothing in config.toml mentions
    the mount paths, so without this file an agent sees an empty home and has
    no reason to look under /workspaces at all.

    The chart mounts this content as AGENTS.md, CLAUDE.md and GEMINI.md in the
    working directory, which is where coding CLIs look for instructions.
    Everything here comes from the catalog, so it cannot drift from the mounts
    that were actually rendered, and it carries no secret values.
    """

    worktree = agent["worktree"]
    role = agent["role"]
    grants = agent["repository_grants"]
    writable = [grant for grant in grants if grant["access"] == "write"]

    lines = [
        f"# {agent_id}",
        "",
        f"你是 OpenAB 多 Agent 編制中的 `{role}`。",
        "",
        "## 你的 workspace",
        "",
        "程式碼**不在家目錄**，在下列掛載點。這是你唯一能存取的程式碼：",
        "",
        "| 路徑 | 存取 |",
        "| --- | --- |",
    ]
    for grant in grants:
        mount = f"{worktree['container_mount_path']}/{grant['checkout_subpath']}"
        lines.append(f"| `{mount}` | {'可讀寫' if grant['access'] == 'write' else '唯讀'} |")

    lines += [
        "",
        "每個都是完整獨立的 git checkout，`origin` 指向交付用的 remote。",
        "",
        "## 邊界",
        "",
        "- 只能在上表列出的路徑內工作；其他路徑未經授權。",
        "- 標記唯讀的掛載由核心強制，寫入會得到 `Read-only file system`——"
        "這不是錯誤，是授權邊界。",
    ]
    if writable:
        lines.append(
            f"- 你有 {len(writable)} 個可寫 workspace。動手前先確認在正確的 task 分支上。"
        )
    else:
        lines.append("- 你沒有任何可寫 workspace；你的職責是閱讀與回報，不是修改程式碼。")
    lines += [
        "- **不要 push，不要開 merge request。** 交付由 leader 依驗收閘門授權。",
        "- 家目錄可寫，適合放暫存筆記；它不是程式碼所在地。",
        "",
    ]
    lines += _workflow_section(agent_id, agent)
    return "\n".join(lines)


def _workflow_section(agent_id: str, agent: Mapping[str, Any]) -> list[str]:
    """Describe the one topology this agent participates in.

    Each agent sees only its own side of the protocol.  A worker that is told
    the leader's rules would be able to reason about instructions it must not
    accept in the first place.
    """

    role = agent["role"]
    discord = agent["discord"]
    lines = ["## 工作流程", ""]

    if role == "leader":
        entry = discord.get("entry_channel_id")
        lines += [
            "整個編制只有你能寫入任務紀錄。worker 不會、也不能自行建立任務。",
            "",
            "```",
            "人類 → 你的 entry channel",
            "         ↓  你建立任務紀錄（planned → assigned）",
            "       worker 的私有頻道（@提及該 worker）",
            "         ↓  worker 執行並回報",
            "       你轉錄回報 → 要求 reviewer 獨立審查",
            "         ↓  蒐集完整證據",
            "       你驗收（accepted）→ 人類授權 merge → 關閉（closed）",
            "```",
            "",
            f"- 你的 entry channel：`{entry}`。只有 allowlist 內的人類能在這裡對你下指令。",
            "- 派工方式：在**該 worker 的私有頻道**發訊息並 @提及它。"
            "worker 只接受來自你、且提及它的訊息；其他一律拒絕。",
            "- 一個 worker 同時只能有一項任務；全域同時最多兩項未結束任務。",
            "",
            "### 驗收閘門（程式任務）",
            "",
            "以下缺一不可，不足就退回，不要放行：",
            "",
            "1. developer 的測試結果",
            "2. reviewer 的獨立審查",
            "3. CI 狀態為 success",
            "4. 你自己的摘要",
            "5. commit SHA、push ref、MR reference",
            "6. 交付的 repository／branch／owner 與任務完全相符",
            "7. **人類明確授權 merge**（含授權者、時間、範圍）",
            "",
            "研究／文件類任務只需要可追溯來源、獨立審查、你的摘要。",
            "",
            "任務被放棄時要記錄取消理由與決定者，不可直接跳到 closed 繞過閘門。",
        ]
    else:
        work = discord.get("work_channel_id")
        lines += [
            "你不會自行決定要做什麼。任務一律由 leader 指派。",
            "",
            "```",
            "leader 在你的私有頻道 @提及你",
            "   ↓",
            "你在授權的 workspace 內執行",
            "   ↓",
            "你回報結果給 leader",
            "   ↓",
            "leader 蒐集證據並決定驗收",
            "```",
            "",
            f"- 你的私有頻道：`{work}`。這是你唯一接收指令的地方。",
            "- **只接受 leader 的訊息，而且必須提及你。** 人類在你的頻道下的指令會被拒絕——"
            "這是刻意的，不要嘗試繞過或代為執行。",
            "- 完成後把結果回報給 leader，由它轉交下一棒。你不直接與其他 worker 協作。",
            "",
        ]
        if role == "developer":
            lines += [
                "### 你的職責",
                "",
                "- 在 leader 指定的 task 分支上實作，不要切到別的分支。",
                "- 跑測試並把結果一併回報——沒有測試結果，leader 無法驗收。",
                "- 回報要包含：改了什麼、測試結果、風險、以及你不確定的地方。",
                "- commit 可以，**push 與開 MR 不行**。那需要人類授權。",
            ]
        elif role == "reviewer":
            lines += [
                "### 你的職責",
                "",
                "- 對 developer 的產出做**獨立**審查。你的 workspace 是唯讀的，這是刻意的。",
                "- 審查要能被 leader 當成驗收證據，所以要具體：哪裡有問題、為什麼、風險多大。",
                "- 沒問題也要明說，不要沉默。",
            ]
        elif role == "researcher":
            lines += [
                "### 你的職責",
                "",
                "- 研究並回報，不修改程式碼。你的 workspace 是唯讀的。",
                "- 結論要附**可追溯的來源**（檔案路徑、行號、commit）。"
                "無法追溯的推測要標明是推測。",
            ]
    lines.append("")
    return lines


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
            # The chart mounts the runtime PVC here, so it must be the image's
            # own user home or the agent gets a read-only root and no writable
            # home.  See runtime.working_dir in docs/參考/catalog-契約.md.
            "workingDir": agent["runtime"].get("working_dir", "/home/agent"),
            "persistence": {"enabled": True, "size": runtime_volume_size},
            "secretEnv": [{"name": "DISCORD_BOT_TOKEN", "secretName": secret_name, "secretKey": secret_key}],
            "configToml": render_config_toml(agent_id, agent),
            # Without this the mounts exist but the agent never learns of them.
            "agentsMd": render_agents_md(agent_id, agent),
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
