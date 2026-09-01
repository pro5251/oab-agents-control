from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from oab_control.environment import validate_environment


def environment(root: Path) -> dict:
    return {
        "version": 1,
        "status": "ready",
        "implementation": {"repository": str(root / "control"), "default_branch": "main"},
        "paths": {
            "catalog": str(root / "catalog.yaml"),
            "coordination_repository": str(root / "coordination"),
            "collection_roots": [str(root / "repositories")],
            "agent_worktrees_root": str(root / "worktrees"),
            "k3s_state": str(root / "k3s"),
            "backup_target": str(root / "backups"),
            "secrets_file": str(root / "secrets.yaml"),
        },
        "gitlab": {"host": "gitlab.example.invalid", "default_base_branch": "origin/develop", "projects": [], "identity_refs": ["gitlab-bootstrap"]},
        "discord": {"server_id": "100000000000000001", "leader_entry_channel_id": "200000000000000001", "human_user_ids": [], "bot_identities": []},
        "profiles": {"leader": {"role": "leader", "bot_secret_ref": "discord-leader/token"}},
        "k3s": {
            "context": "oab-agents",
            "deployer_kubeconfig_env": "KUBECONFIG",
            "secret_materializer_kubeconfig_env": "OAB_SECRET_MATERIALIZER_KUBECONFIG",
            "secrets_encryption_enabled": True,
            "secrets_encryption_recovery_ref": "operator://k3s-secrets-recovery",
            "network_policy_controller": "kube-router",
        },
        "pending_decisions": [],
    }


class EnvironmentTests(unittest.TestCase):
    def test_ready_contract_requires_existing_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for path in ("control", "coordination", "repositories", "worktrees", "k3s", "backups"):
                (root / path).mkdir()
            (root / "catalog.yaml").write_text("version: 1\n", encoding="utf-8")
            errors = validate_environment(environment(root))
        self.assertEqual(errors, [])

    def test_bootstrap_pending_is_explicitly_not_ready(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            document = environment(Path(directory))
            document["status"] = "bootstrap-pending"
            document["pending_decisions"] = [{"key": "discord.ids", "owner": "operator", "placeholder_policy": "numeric IDs only"}]
            errors = validate_environment(document, require_ready=True)
        self.assertTrue(any(error.code == "not_ready" for error in errors))

    def test_rejects_credential_value_and_unknown_field(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            document = environment(Path(directory))
            document["discord"]["bot_token"] = "glpat-123456789012345678"
            document["gitlab"]["unexpected"] = True
            errors = validate_environment(document)
        codes = {error.code for error in errors}
        self.assertIn("secret_value", codes)
        self.assertIn("unknown_field", codes)

    def test_ready_contract_requires_separate_secret_materializer_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            document = environment(Path(directory))
            document["k3s"].pop("secret_materializer_kubeconfig_env")
            missing = validate_environment(document)
            document["k3s"]["secret_materializer_kubeconfig_env"] = "KUBECONFIG"
            shared = validate_environment(document)
        self.assertTrue(any(error.path.endswith("secret_materializer_kubeconfig_env") and error.code == "required" for error in missing))
        self.assertTrue(any(error.code == "separate_identity" for error in shared))


if __name__ == "__main__":
    unittest.main()
