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


if __name__ == "__main__":
    unittest.main()
