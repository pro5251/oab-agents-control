from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from oab_control.catalog import load_catalog, load_reference_manifest, normalized_json, validate_catalog
from oab_control.cli import main
from oab_control.plan import render_plan


IDS = {
    "leader": ("100000000000000001", "200000000000000001"),
    "researcher": ("100000000000000002", "200000000000000002"),
    "developer": ("100000000000000003", "200000000000000003"),
    "reviewer": ("100000000000000004", "200000000000000004"),
}


def catalog(root: Path) -> dict:
    repo = root / "repositories" / "team-a" / "service-x"
    repo.mkdir(parents=True)
    (repo / ".git").mkdir()
    agents = {}
    roles = ("leader", "researcher", "developer", "reviewer")
    for index, role in enumerate(roles):
        bot, channel = IDS[role]
        discord = {
            "bot_secret_ref": f"discord-{role}/token",
            "bot_user_id": bot,
            "allow_all_channels": False,
            "allow_all_users": False,
            "allowed_users": ["300000000000000001"] if role == "leader" else [],
            "allow_bot_messages": "mentions",
            "allow_user_messages": "multibot-mentions",
            "trusted_bot_ids": [],
        }
        if role == "leader":
            discord["entry_channel_id"] = channel
        else:
            discord["work_channel_id"] = channel
        agents[role] = {
            "role": role,
            "runtime": {"command": "openab", "args": ["--acp"], "model": "model-a", "image": "registry/openab:dev"},
            "discord": discord,
            "worktree": {
                "path": str(root / "worktrees" / role),
                "container_mount_path": f"/workspaces/{role}",
                "collection_roots": [str(root / "repositories")],
            },
            "repository_grants": [{
                "repository": str(repo),
                "checkout_subpath": "team-a/service-x",
                "access": "read" if role in {"researcher", "reviewer"} else "write",
            }],
            "delivery": {"gitlab_identity_ref": "gitlab-bootstrap"},
            "egress_grants": ["discord", "gitlab", "model-provider"],
            "resources": {
                "requests": {"cpu": "100m", "memory": "256Mi"},
                "limits": {"cpu": "1", "memory": "1Gi"},
            },
        }
    agents["leader"]["discord"]["trusted_bot_ids"] = [IDS[role][0] for role in roles if role != "leader"]
    for role in roles:
        if role != "leader":
            agents[role]["discord"]["trusted_bot_ids"] = [IDS["leader"][0]]
    return {"version": 1, "defaults": {"base_branch": "origin/develop", "human_access": "deny", "bot_message_mode": "mentions"}, "agents": agents}


class CatalogValidationTests(unittest.TestCase):
    def test_four_profiles_normalize_and_default_branch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            document = catalog(Path(directory))
            normalized, diagnostics = validate_catalog(document, check_git=True)
            normalized_again, diagnostics_again = validate_catalog(deepcopy(document), check_git=True)
        self.assertEqual(diagnostics, [])
        self.assertEqual(diagnostics_again, [])
        assert normalized is not None
        assert normalized_again is not None
        self.assertEqual(normalized["agents"]["developer"]["repository_grants"][0]["base_branch"], "origin/develop")
        self.assertEqual(normalized_json(normalized), normalized_json(normalized_again))

    def test_rejects_worker_human_access(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            document = catalog(Path(directory))
            document["agents"]["developer"]["discord"]["allowed_users"] = ["300000000000000001"]
            _, diagnostics = validate_catalog(document)
        self.assertTrue(any(d.code == "human_access" for d in diagnostics))

    def test_rejects_open_channel_and_logical_trust_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            document = catalog(Path(directory))
            document["agents"]["developer"]["discord"]["allow_all_channels"] = True
            document["agents"]["developer"]["discord"]["trusted_bot_ids"] = ["leader"]
            _, diagnostics = validate_catalog(document)
        codes = {d.code for d in diagnostics}
        self.assertIn("default_deny", codes)
        self.assertIn("discord_id", codes)

    def test_rejects_shell_syntax_in_runtime_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            document = catalog(Path(directory))
            document["agents"]["developer"]["runtime"]["command"] = "sh -c 'openab'"
            _, diagnostics = validate_catalog(document)
        self.assertTrue(any(d.code == "runtime_command" for d in diagnostics))

    def test_rejects_path_escape_and_source_mount_field(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            document = catalog(Path(directory))
            document["agents"]["developer"]["repository_grants"][0]["checkout_subpath"] = "../shared"
            document["agents"]["developer"]["worktree"]["source_mount"] = str(Path(directory))
            _, diagnostics = validate_catalog(document)
        codes = {d.code for d in diagnostics}
        self.assertIn("unsafe_subpath", codes)
        self.assertIn("unknown_field", codes)

    def test_rejects_symlinked_repository_outside_collection_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document = catalog(root)
            outside = root / "outside"
            outside.mkdir()
            (outside / ".git").mkdir()
            link = root / "repositories" / "escaped"
            link.symlink_to(outside, target_is_directory=True)
            document["agents"]["developer"]["repository_grants"][0]["repository"] = str(link)
            _, diagnostics = validate_catalog(document, check_paths=True)
        self.assertTrue(any(d.code == "collection_boundary" for d in diagnostics))

    def test_duplicate_yaml_keys_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.yaml"
            path.write_text("version: 1\nversion: 1\nagents: {}\n", encoding="utf-8")
            _, diagnostics = load_catalog(path)
        self.assertEqual([diagnostic.code for diagnostic in diagnostics], ["load"])

    def test_rejects_duplicate_worktree_and_secret_value(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            document = catalog(Path(directory))
            document["agents"]["reviewer"]["worktree"]["path"] = document["agents"]["developer"]["worktree"]["path"]
            document["agents"]["reviewer"]["runtime"]["args"] = ["--token=glpat-123456789012345678"]
            _, diagnostics = validate_catalog(document)
        codes = {d.code for d in diagnostics}
        self.assertIn("path_overlap", codes)
        self.assertIn("secret_value", codes)

    def test_rejects_shared_discord_secret_reference(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            document = catalog(Path(directory))
            document["agents"]["reviewer"]["discord"]["bot_secret_ref"] = document["agents"]["developer"]["discord"]["bot_secret_ref"]
            _, diagnostics = validate_catalog(document)
        self.assertTrue(any(d.code == "duplicate_secret_ref" for d in diagnostics))

    def test_reference_manifest_fails_closed_for_unresolved_refs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document = catalog(root)
            secret_refs = {"discord-leader/token", "discord-researcher/token", "discord-developer/token", "discord-reviewer/token"}
            _, diagnostics = validate_catalog(
                document,
                available_secret_refs=secret_refs,
                available_identity_refs={"gitlab-bootstrap"},
            )
            manifest = root / "refs.yaml"
            manifest.write_text("secret_refs: [discord-leader/token]\nidentity_refs: [gitlab-bootstrap]\n", encoding="utf-8")
            loaded_secrets, loaded_identities, manifest_errors = load_reference_manifest(manifest)
        self.assertEqual(diagnostics, [])
        self.assertEqual(loaded_secrets, {"discord-leader/token"})
        self.assertEqual(loaded_identities, {"gitlab-bootstrap"})
        self.assertEqual(manifest_errors, [])

        with tempfile.TemporaryDirectory() as directory:
            document = catalog(Path(directory))
            _, diagnostics = validate_catalog(document, available_secret_refs={"discord-leader/token"}, available_identity_refs=set())
        self.assertTrue(any(d.code == "unresolved_ref" for d in diagnostics))

    def test_cli_json_is_stable_and_does_not_echo_bad_secret(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.yaml"
            path.write_text("version: 1\nagents: {}\n", encoding="utf-8")
            from io import StringIO
            import contextlib

            stdout = StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(["validate", str(path), "--json"])
            payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertFalse(payload["valid"])
        self.assertNotIn("secret", stdout.getvalue().lower())

    def test_plan_projects_one_isolated_workload_per_agent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            normalized, diagnostics = validate_catalog(catalog(Path(directory)))
        self.assertEqual(diagnostics, [])
        assert normalized is not None
        plan = render_plan(normalized, namespace="oab-agents-test")
        self.assertEqual(plan["namespace"], "oab-agents-test")
        self.assertEqual(len(plan["workloads"]), 4)
        developer = next(item for item in plan["workloads"] if item["agent_id"] == "developer")
        self.assertFalse(developer["worktree_mounts"][0]["read_only"])
        researcher = next(item for item in plan["workloads"] if item["agent_id"] == "researcher")
        self.assertTrue(researcher["worktree_mounts"][0]["read_only"])
        self.assertTrue(plan["apply"]["requires_human_confirmation"])
        self.assertFalse(plan["apply"]["mutates_cluster"])
        self.assertTrue(plan["network_policy"]["default_deny_egress"])
        self.assertFalse(plan["rbac"]["agent_cluster_admin"])


if __name__ == "__main__":
    unittest.main()
