from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest

import yaml

from oab_control.cli import main
from test_catalog import catalog
from test_tasks import task
from test_worktree import repository_fixture


class CliOperationTests(unittest.TestCase):
    def test_deploy_cli_requires_confirmation_before_apply(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog_path = root / "catalog.yaml"
            catalog_path.write_text(yaml.safe_dump(catalog(root), sort_keys=False), encoding="utf-8")
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = main(["deploy", str(catalog_path), "--snapshot-dir", str(root / "snapshots"), "--no-path-check", "--json"])
            result = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertFalse(result["applied"])
        self.assertFalse(result["plan"]["apply"]["mutates_cluster"])

    def test_confirmed_deploy_without_chart_is_safe_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog_path = root / "catalog.yaml"
            catalog_path.write_text(yaml.safe_dump(catalog(root), sort_keys=False), encoding="utf-8")
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = main(["deploy", str(catalog_path), "--snapshot-dir", str(root / "snapshots"), "--no-path-check", "--yes", "--json"])
            result = json.loads(output.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertIn("environment", result["error"])

    def test_preflight_cli_is_read_only_and_reports_not_ready(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment_path = root / "environment.yaml"
            environment_path.write_text("version: 1\nstatus: bootstrap-pending\n", encoding="utf-8")
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = main(["preflight", str(environment_path), "--chart", str(root / "chart"), "--json"])
            result = json.loads(output.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertTrue(result["read_only"])
        self.assertFalse(result["ready"])

    def test_status_cli_returns_machine_readable_local_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog_path = root / "catalog.yaml"
            catalog_path.write_text(yaml.safe_dump(catalog(root), sort_keys=False), encoding="utf-8")
            output = io.StringIO()
            with redirect_stdout(output), redirect_stderr(io.StringIO()):
                exit_code = main(["status", str(catalog_path), "--json"])
            result = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(result["runtime"]["state"], "unknown")
        self.assertEqual(len(result["plan"]["workloads"]), 4)

    def test_task_cli_uses_same_durable_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tasks_dir = root / "tasks"
            task_file = root / "task.json"
            document = catalog(root)
            catalog_path = root / "catalog.yaml"
            catalog_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
            developer = document["agents"]["developer"]
            grant = developer["repository_grants"][0]
            task_document = task().as_dict() | {
                "repository": grant["repository"],
                "checkout_subpath": grant["checkout_subpath"],
                "worktree_path": developer["worktree"]["path"],
                "container_mount_path": f"{developer['worktree']['container_mount_path']}/{grant['checkout_subpath']}",
                "base_branch": "origin/develop",
                "gitlab_identity_ref": developer["delivery"]["gitlab_identity_ref"],
                "reply_to": developer["discord"]["work_channel_id"],
            }
            task_file.write_text(json.dumps(task_document), encoding="utf-8")
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["task-create", str(task_file), "--catalog", str(catalog_path), "--tasks-dir", str(tasks_dir), "--no-path-check", "--json"]), 0)
                self.assertEqual(main(["task-transition", "task-001", "assigned", "--tasks-dir", str(tasks_dir), "--json"]), 0)
            listing_output = io.StringIO()
            with redirect_stdout(listing_output):
                exit_code = main(["task-list", "--tasks-dir", str(tasks_dir), "--json"])
            result = json.loads(listing_output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(result["tasks"][0]["state"], "assigned")

    def test_task_create_cli_rejects_code_task_without_developer_write_grant(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document = catalog(root)
            catalog_path = root / "catalog.yaml"
            catalog_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
            researcher = document["agents"]["researcher"]
            grant = researcher["repository_grants"][0]
            task_document = task().as_dict() | {
                "agent_id": "researcher",
                "delivery_owner": "researcher",
                "repository": grant["repository"],
                "checkout_subpath": grant["checkout_subpath"],
                "worktree_path": researcher["worktree"]["path"],
                "container_mount_path": f"{researcher['worktree']['container_mount_path']}/{grant['checkout_subpath']}",
                "base_branch": "origin/develop",
                "gitlab_identity_ref": researcher["delivery"]["gitlab_identity_ref"],
                "reply_to": researcher["discord"]["work_channel_id"],
            }
            task_file = root / "task.json"
            task_file.write_text(json.dumps(task_document), encoding="utf-8")
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = main(["task-create", str(task_file), "--catalog", str(catalog_path), "--no-path-check", "--json"])
            result = json.loads(output.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertIn("developer agent", result["error"])

    def _local_remote_fixture(self, root: Path) -> tuple[Path, Path, Path]:
        """A validatable catalog whose grant points at a real repo with a file:// remote."""

        document = catalog(root)
        shutil.rmtree(root / "repositories" / "team-a" / "service-x")
        source, remote = repository_fixture(root)
        catalog_path = root / "catalog.yaml"
        catalog_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
        remotes_path = root / "remotes.json"
        remotes_path.write_text(json.dumps({str(source): remote.as_uri()}), encoding="utf-8")
        return catalog_path, remotes_path, source

    def test_materialize_rejects_local_remote_without_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog_path, remotes_path, _ = self._local_remote_fixture(root)
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = main([
                    "worktree-materialize", str(catalog_path), "developer", "task-001",
                    "--remotes-file", str(remotes_path), "--tasks-dir", "", "--json",
                ])
            result = json.loads(output.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertIn("local path", result["error"])

    def test_materialize_accepts_local_remote_when_explicitly_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog_path, remotes_path, source = self._local_remote_fixture(root)
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = main([
                    "worktree-materialize", str(catalog_path), "developer", "task-001",
                    "--remotes-file", str(remotes_path), "--allow-local-remotes",
                    "--tasks-dir", "", "--json",
                ])
            result = json.loads(output.getvalue())
            checkout = result["checkouts"][0]
            self.assertTrue(checkout["created"])
            self.assertTrue(checkout["origin"].startswith("file://"))
            self.assertTrue((Path(checkout["path"]) / ".git").is_dir())
            # The opt-in must not weaken worktree isolation: the checkout is a
            # full clone, never the shared source collection directory.
            self.assertNotEqual(Path(checkout["path"]).resolve(), source.resolve())
        self.assertEqual(exit_code, 0)


if __name__ == "__main__":
    unittest.main()
