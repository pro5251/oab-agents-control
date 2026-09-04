from __future__ import annotations

from pathlib import Path
import tempfile
import tomllib
import unittest

import yaml

from oab_control.catalog import validate_catalog
from oab_control.renderer import render_config_toml, render_openab_values, render_openab_values_yaml
from test_catalog import catalog


class RendererTests(unittest.TestCase):
    def normalized(self) -> dict:
        with tempfile.TemporaryDirectory() as directory:
            value, diagnostics = validate_catalog(catalog(Path(directory)))
        self.assertEqual(diagnostics, [])
        assert value is not None
        return value

    def test_config_toml_uses_secret_env_and_pool_model(self) -> None:
        normalized = self.normalized()
        config = tomllib.loads(render_config_toml("developer", normalized["agents"]["developer"]))
        self.assertEqual(config["discord"]["allow_all_channels"], False)
        self.assertEqual(config["discord"]["allowed_channels"], ["200000000000000003"])
        self.assertEqual(config["agent"]["command"], "openab")
        self.assertEqual(config["pool"]["default_config_options"]["model"], "model-a")
        self.assertNotIn("glpat-", render_config_toml("developer", normalized["agents"]["developer"]))

    def test_values_render_one_agent_with_narrow_mounts(self) -> None:
        normalized = self.normalized()
        values = render_openab_values(normalized, runtime_volume_size="8Gi")
        self.assertEqual(set(values["agents"]), {"kiro", "leader", "researcher", "developer", "reviewer"})
        self.assertFalse(values["agents"]["kiro"]["enabled"])
        developer = values["agents"]["developer"]
        self.assertEqual(developer["persistence"], {"enabled": True, "size": "8Gi"})
        self.assertEqual(developer["secretEnv"], [{"name": "DISCORD_BOT_TOKEN", "secretName": "discord-developer", "secretKey": "token"}])
        self.assertFalse(developer["extraVolumeMounts"][0]["readOnly"])
        researcher = values["agents"]["researcher"]
        self.assertTrue(researcher["extraVolumeMounts"][0]["readOnly"])
        self.assertNotIn("collection_roots", developer["configToml"])
        self.assertEqual(values["podSecurityContext"]["seccompProfile"], {"type": "RuntimeDefault"})
        self.assertTrue(values["containerSecurityContext"]["readOnlyRootFilesystem"])
        self.assertFalse(values["containerSecurityContext"]["allowPrivilegeEscalation"])
        self.assertEqual(values["containerSecurityContext"]["capabilities"]["drop"], ["ALL"])

    def test_yaml_render_is_round_trippable(self) -> None:
        values = yaml.safe_load(render_openab_values_yaml(self.normalized()))
        self.assertEqual(values["agents"]["leader"]["nameOverride"], "leader")
        self.assertIn('bot_token = "${DISCORD_BOT_TOKEN}"', values["agents"]["leader"]["configToml"])

    def test_working_dir_defaults_to_the_native_image_home(self) -> None:
        normalized = self.normalized()
        developer = normalized["agents"]["developer"]
        self.assertEqual(developer["runtime"]["working_dir"], "/home/agent")
        config = tomllib.loads(render_config_toml("developer", developer))
        self.assertEqual(config["agent"]["working_dir"], "/home/agent")
        values = render_openab_values(normalized)
        self.assertEqual(values["agents"]["developer"]["workingDir"], "/home/agent")

    def test_working_dir_follows_the_image_variant_per_agent(self) -> None:
        """A -claude/-gemini image runs as `node`, so its home differs."""

        with tempfile.TemporaryDirectory() as directory:
            document = catalog(Path(directory))
            document["agents"]["developer"]["runtime"]["working_dir"] = "/home/node"
            normalized, diagnostics = validate_catalog(document)
        self.assertEqual(diagnostics, [])
        assert normalized is not None

        config = tomllib.loads(render_config_toml("developer", normalized["agents"]["developer"]))
        self.assertEqual(config["agent"]["working_dir"], "/home/node")
        values = render_openab_values(normalized)
        self.assertEqual(values["agents"]["developer"]["workingDir"], "/home/node")
        # Per-agent, so an agent that did not opt in keeps the default.
        self.assertEqual(values["agents"]["reviewer"]["workingDir"], "/home/agent")

    def test_agents_md_lists_every_mount_the_agent_actually_gets(self) -> None:
        """A mounted checkout the agent is never told about is unreachable."""

        normalized = self.normalized()
        values = render_openab_values(normalized)
        for agent_id, agent in normalized["agents"].items():
            document = values["agents"][agent_id]["agentsMd"]
            worktree = agent["worktree"]
            for grant in agent["repository_grants"]:
                mount = f"{worktree['container_mount_path']}/{grant['checkout_subpath']}"
                self.assertIn(mount, document, f"{agent_id} 未被告知 {mount}")
            # Every mount named in the file is one the values actually declare.
            declared = {item["mountPath"] for item in values["agents"][agent_id]["extraVolumeMounts"]}
            for line in document.splitlines():
                if line.startswith("| `/"):
                    path = line.split("`")[1]
                    self.assertIn(path, declared)

    def test_agents_md_states_the_access_the_mount_enforces(self) -> None:
        normalized = self.normalized()
        values = render_openab_values(normalized)
        writer = values["agents"]["developer"]["agentsMd"]
        reader = values["agents"]["reviewer"]["agentsMd"]
        self.assertIn("可讀寫", writer)
        self.assertNotIn("可讀寫", reader)
        self.assertIn("唯讀", reader)
        # A read-only agent should not be told to mind its task branch.
        self.assertIn("你沒有任何可寫 workspace", reader)

    def test_workflow_tells_each_agent_only_its_own_side(self) -> None:
        """A worker that knows the leader's rules can reason about instructions
        it is supposed to refuse outright."""

        values = render_openab_values(self.normalized())
        leader = values["agents"]["leader"]["agentsMd"]
        worker = values["agents"]["developer"]["agentsMd"]

        # The leader owns dispatch and the acceptance criteria.
        self.assertIn("只有你能寫入任務紀錄", leader)
        self.assertIn("### 驗收閘門（程式任務）", leader)
        self.assertIn("人類明確授權 merge", leader)

        # A worker is told to refuse humans.  It may know that gates exist --
        # that is why it must not push -- but not what satisfies them, which
        # is the leader's judgement to make.
        self.assertIn("只接受 leader 的訊息", worker)
        self.assertNotIn("### 驗收閘門（程式任務）", worker)
        self.assertNotIn("人類明確授權 merge", worker)
        self.assertNotIn("你建立任務紀錄", worker)

    def test_workflow_names_the_channel_the_catalog_actually_configured(self) -> None:
        normalized = self.normalized()
        values = render_openab_values(normalized)
        for agent_id, agent in normalized["agents"].items():
            discord = agent["discord"]
            expected = discord["entry_channel_id"] if agent["role"] == "leader" else discord["work_channel_id"]
            self.assertIn(expected, values["agents"][agent_id]["agentsMd"])

    def test_workflow_forbids_push_for_every_role(self) -> None:
        values = render_openab_values(self.normalized())
        for agent_id, agent in values["agents"].items():
            if agent_id == "kiro":
                continue
            self.assertIn("不要 push", agent["agentsMd"], agent_id)

    def test_agents_md_carries_no_secret_reference_values(self) -> None:
        values = render_openab_values(self.normalized())
        for agent in values["agents"].values():
            document = agent.get("agentsMd", "")
            self.assertNotIn("DISCORD_BOT_TOKEN", document)
            self.assertNotIn("discord-", document)

    def test_unsafe_working_dir_is_rejected(self) -> None:
        for unsafe in ("home/node", "~/agent", "/home/${USER}", "/home/../etc"):
            with self.subTest(working_dir=unsafe), tempfile.TemporaryDirectory() as directory:
                document = catalog(Path(directory))
                document["agents"]["developer"]["runtime"]["working_dir"] = unsafe
                normalized, diagnostics = validate_catalog(document)
                self.assertIsNone(normalized)
                self.assertTrue(
                    any(item.path == "$.agents.developer.runtime.working_dir" for item in diagnostics),
                    diagnostics,
                )


if __name__ == "__main__":
    unittest.main()
