from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml

from oab_control.operations import ControlOperations, OperationError, _kubectl_apply
from oab_control.registry import WorkspaceRecord, WorkspaceRegistry
from oab_control.tasks import Task, TaskStore
from test_catalog import catalog


class OperationsTests(unittest.TestCase):
    def write_catalog(self, root: Path) -> Path:
        path = root / "catalog.yaml"
        path.write_text(yaml.safe_dump(catalog(root), sort_keys=False), encoding="utf-8")
        return path

    def write_ready_environment(self, root: Path, catalog_path: Path) -> Path:
        for name in ("control", "coordination", "repositories", "worktrees", "k3s", "backups", "external-backups"):
            (root / name).mkdir(exist_ok=True)
        document = {
            "version": 1,
            "status": "ready",
            "implementation": {"repository": str(root / "control"), "default_branch": "main"},
            "paths": {
                "catalog": str(catalog_path),
                "coordination_repository": str(root / "coordination"),
                "collection_roots": [str(root / "repositories")],
                "agent_worktrees_root": str(root / "worktrees"),
                "k3s_state": str(root / "k3s"),
                "backup_target": str(root / "external-backups"),
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
        path = root / "environment.yaml"
        path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
        return path

    def backup_kwargs(self, root: Path, catalog_path: Path) -> dict[str, object]:
        """Return the explicit external-backup inputs required by confirmed deploy."""
        return {
            "backup_sources": {
                "catalog": catalog_path,
                "coordination_repository": root / "coordination",
                "agent_worktrees_root": root / "worktrees",
                "k3s_state": root / "k3s",
            },
            "backup_output": root / "external-backups",
            "backup_attestation": "operator verified encrypted test target",
        }

    def test_unconfirmed_deploy_snapshots_and_never_calls_apply(self) -> None:
        calls: list[tuple[str, str, str, str]] = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog_path = self.write_catalog(root)
            result = ControlOperations(namespace="oab-agents-test").deploy(
                catalog_path=catalog_path,
                snapshot_dir=root / "backups",
                confirmed=False,
                check_paths=False,
                apply=lambda chart, values, release, namespace: calls.append((str(chart), str(values), release, namespace)) or "applied",
            )
            snapshot = Path(result["snapshot"])
            snapshot_files_exist = (snapshot / "catalog.yaml").is_file() and (snapshot / "openab-values.yaml").is_file()
            applied_in_metadata = json.loads((snapshot / "metadata.json").read_text(encoding="utf-8"))["k3s_apply_performed"]
        self.assertEqual(calls, [])
        self.assertFalse(result["applied"])
        self.assertIn("human confirmation required", result["reason"])
        self.assertTrue(snapshot_files_exist)
        self.assertFalse(applied_in_metadata)

    def test_default_kubectl_apply_invokes_one_manifest_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "isolation.yaml"
            manifest.write_text("apiVersion: v1\nkind: ConfigMap\n", encoding="utf-8")
            with patch("oab_control.operations.subprocess.run") as run:
                run.return_value.stdout = "configmap/example configured\n"
                result = _kubectl_apply(manifest)
        self.assertEqual(result, "configmap/example configured\n")
        self.assertEqual(run.call_args.args, (["kubectl", "apply", "--filename", str(manifest)],))
        self.assertTrue(run.call_args.kwargs["check"])
        self.assertTrue(run.call_args.kwargs["capture_output"])

    def test_confirmed_deploy_calls_injected_apply_only_after_snapshot(self) -> None:
        calls: list[tuple[str, str, str, str]] = []
        k8s_calls: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog_path = self.write_catalog(root)
            chart = root / "openab-chart"
            chart.mkdir()
            (chart / "Chart.yaml").write_text("apiVersion: v2\nname: test-chart\nversion: 0.1.0\n", encoding="utf-8")
            environment_path = self.write_ready_environment(root, catalog_path)
            result = ControlOperations().deploy(
                catalog_path=catalog_path,
                snapshot_dir=root / "backups",
                environment_file=environment_path,
                chart_path=chart,
                confirmed=True,
                check_paths=False,
                apply=lambda chart_path, values, release, namespace: calls.append((str(chart_path), str(values), release, namespace)) or "ok",
                k8s_apply=lambda manifest: k8s_calls.append(str(manifest)) or "k8s-ok",
                **self.backup_kwargs(root, catalog_path),
            )
            metadata = json.loads((Path(result["snapshot"]) / "metadata.json").read_text(encoding="utf-8"))
        self.assertTrue(result["applied"])
        self.assertEqual(len(k8s_calls), 1)
        self.assertEqual(calls[0][2:], ("oab-agents", "oab-agents"))
        self.assertTrue(metadata["k3s_apply_performed"])

    def test_confirmed_deploy_forwards_configured_deployer_kubeconfig_to_default_apply(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog_path = self.write_catalog(root)
            chart = root / "openab-chart"
            chart.mkdir()
            (chart / "Chart.yaml").write_text("apiVersion: v2\nname: test-chart\nversion: 0.1.0\n", encoding="utf-8")
            environment_path = self.write_ready_environment(root, catalog_path)
            environment = yaml.safe_load(environment_path.read_text(encoding="utf-8"))
            environment["k3s"]["deployer_kubeconfig_env"] = "OAB_DEPLOYER_KUBECONFIG"
            environment_path.write_text(yaml.safe_dump(environment, sort_keys=False), encoding="utf-8")
            with patch.dict("oab_control.operations.os.environ", {"OAB_DEPLOYER_KUBECONFIG": "/secure/deployer-kubeconfig"}, clear=False):
                with patch("oab_control.operations._kubectl_apply", return_value="k8s-ok") as kubectl_apply:
                    with patch("oab_control.operations._helm_apply", return_value="helm-ok") as helm_apply:
                        result = ControlOperations().deploy(
                            catalog_path=catalog_path,
                            snapshot_dir=root / "backups",
                            environment_file=environment_path,
                            chart_path=chart,
                            confirmed=True,
                            check_paths=False,
                            **self.backup_kwargs(root, catalog_path),
                        )
        self.assertTrue(result["applied"])
        self.assertEqual(kubectl_apply.call_args.kwargs["kubeconfig"], "/secure/deployer-kubeconfig")
        self.assertEqual(helm_apply.call_args.kwargs["kubeconfig"], "/secure/deployer-kubeconfig")

    def test_failed_confirmed_deploy_marks_snapshot_without_echoing_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog_path = self.write_catalog(root)
            chart = root / "openab-chart"
            chart.mkdir()
            environment_path = self.write_ready_environment(root, catalog_path)
            with self.assertRaises(OperationError):
                ControlOperations().deploy(
                    catalog_path=catalog_path,
                    snapshot_dir=root / "backups",
                    environment_file=environment_path,
                    chart_path=chart,
                    confirmed=True,
                    check_paths=False,
                    apply=lambda *_: (_ for _ in ()).throw(RuntimeError("secret token should not be persisted")),
                    k8s_apply=lambda *_: "k8s-ok",
                    **self.backup_kwargs(root, catalog_path),
                )
            snapshots = list((root / "backups").glob("snapshot-*"))
            metadata = json.loads((snapshots[0] / "metadata.json").read_text(encoding="utf-8"))
        self.assertTrue(metadata["apply_failed"])
        self.assertEqual(metadata["apply_failure_type"], "RuntimeError")
        self.assertNotIn("secret token", json.dumps(metadata))

    def test_failed_helm_records_that_k3s_already_changed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog_path = self.write_catalog(root)
            chart = root / "openab-chart"
            chart.mkdir()
            environment_path = self.write_ready_environment(root, catalog_path)
            with self.assertRaises(OperationError):
                ControlOperations().deploy(
                    catalog_path=catalog_path,
                    snapshot_dir=root / "backups",
                    environment_file=environment_path,
                    chart_path=chart,
                    confirmed=True,
                    check_paths=False,
                    apply=lambda *_: (_ for _ in ()).throw(RuntimeError("helm failed")),
                    k8s_apply=lambda *_: "k8s-ok",
                    **self.backup_kwargs(root, catalog_path),
                )
            snapshot = next((root / "backups").glob("snapshot-*"))
            metadata = json.loads((snapshot / "metadata.json").read_text(encoding="utf-8"))
        self.assertTrue(metadata["k3s_apply_performed"])
        self.assertFalse(metadata["helm_apply_performed"])
        self.assertTrue(metadata["apply_failed"])

    def test_confirmed_deploy_materializes_local_secret_values_without_persisting_them_in_snapshot(self) -> None:
        secret_calls: list[list[dict]] = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog_path = self.write_catalog(root)
            chart = root / "openab-chart"
            chart.mkdir()
            environment_path = self.write_ready_environment(root, catalog_path)
            secrets_path = root / "secrets.yaml"
            refs = {
                "discord-leader/token": "leader-value",
                "discord-researcher/token": "researcher-value",
                "discord-developer/token": "developer-value",
                "discord-reviewer/token": "reviewer-value",
            }
            secrets_path.write_text(yaml.safe_dump({"secrets": refs, "identity_refs": ["gitlab-bootstrap"]}), encoding="utf-8")
            secrets_path.chmod(0o600)
            result = ControlOperations().deploy(
                catalog_path=catalog_path,
                snapshot_dir=root / "backups",
                environment_file=environment_path,
                secrets_file=secrets_path,
                chart_path=chart,
                confirmed=True,
                check_paths=False,
                apply=lambda *_: "helm-ok",
                k8s_apply=lambda *_: "k8s-ok",
                secret_apply=lambda manifests: secret_calls.append(manifests) or "secrets-ok",
                **self.backup_kwargs(root, catalog_path),
            )
            snapshot_files = [path.read_text(encoding="utf-8") for path in Path(result["snapshot"]).iterdir() if path.is_file()]
        self.assertEqual(result["secret_apply_output"], "secrets-ok")
        self.assertEqual(len(secret_calls[0]), 4)
        self.assertTrue(all(value not in "\n".join(snapshot_files) for value in refs.values()))

    def test_default_secret_apply_uses_separate_materializer_kubeconfig(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog_path = self.write_catalog(root)
            chart = root / "openab-chart"
            chart.mkdir()
            (chart / "Chart.yaml").write_text("apiVersion: v2\nname: test-chart\nversion: 0.1.0\n", encoding="utf-8")
            environment_path = self.write_ready_environment(root, catalog_path)
            environment = yaml.safe_load(environment_path.read_text(encoding="utf-8"))
            environment["k3s"]["deployer_kubeconfig_env"] = "OAB_DEPLOYER_KUBECONFIG"
            environment["k3s"]["secret_materializer_kubeconfig_env"] = "OAB_SECRET_MATERIALIZER_KUBECONFIG"
            environment_path.write_text(yaml.safe_dump(environment, sort_keys=False), encoding="utf-8")
            secrets_path = root / "secrets.yaml"
            secrets_path.write_text(
                yaml.safe_dump(
                    {
                        "secrets": {
                            "discord-leader/token": "leader-value",
                            "discord-researcher/token": "researcher-value",
                            "discord-developer/token": "developer-value",
                            "discord-reviewer/token": "reviewer-value",
                        },
                        "identity_refs": ["gitlab-bootstrap"],
                    }
                ),
                encoding="utf-8",
            )
            secrets_path.chmod(0o600)
            with patch.dict(
                "oab_control.operations.os.environ",
                {
                    "OAB_DEPLOYER_KUBECONFIG": "/secure/deployer-kubeconfig",
                    "OAB_SECRET_MATERIALIZER_KUBECONFIG": "/secure/secret-materializer-kubeconfig",
                },
                clear=False,
            ):
                with patch("oab_control.operations._kubectl_apply_secrets", return_value="secrets-ok") as secret_apply:
                    with patch("oab_control.operations._kubectl_apply", return_value="k8s-ok") as kubectl_apply:
                        with patch("oab_control.operations._helm_apply", return_value="helm-ok") as helm_apply:
                            result = ControlOperations().deploy(
                                catalog_path=catalog_path,
                                snapshot_dir=root / "backups",
                                environment_file=environment_path,
                                secrets_file=secrets_path,
                                chart_path=chart,
                                confirmed=True,
                                check_paths=False,
                                **self.backup_kwargs(root, catalog_path),
                            )
        self.assertTrue(result["applied"])
        self.assertEqual(secret_apply.call_args.kwargs["kubeconfig"], "/secure/secret-materializer-kubeconfig")
        self.assertEqual(kubectl_apply.call_args.kwargs["kubeconfig"], "/secure/deployer-kubeconfig")
        self.assertEqual(helm_apply.call_args.kwargs["kubeconfig"], "/secure/deployer-kubeconfig")

    def test_default_secret_apply_requires_materializer_kubeconfig_before_backup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog_path = self.write_catalog(root)
            chart = root / "openab-chart"
            chart.mkdir()
            (chart / "Chart.yaml").write_text("apiVersion: v2\nname: test-chart\nversion: 0.1.0\n", encoding="utf-8")
            environment_path = self.write_ready_environment(root, catalog_path)
            secrets_path = root / "secrets.yaml"
            secrets_path.write_text(
                yaml.safe_dump(
                    {
                        "secrets": {
                            "discord-leader/token": "leader-value",
                            "discord-researcher/token": "researcher-value",
                            "discord-developer/token": "developer-value",
                            "discord-reviewer/token": "reviewer-value",
                        },
                        "identity_refs": ["gitlab-bootstrap"],
                    }
                ),
                encoding="utf-8",
            )
            secrets_path.chmod(0o600)
            with patch.dict("oab_control.operations.os.environ", {"KUBECONFIG": "/secure/deployer-kubeconfig"}, clear=False):
                with self.assertRaisesRegex(OperationError, "Secret materializer"):
                    ControlOperations().deploy(
                        catalog_path=catalog_path,
                        snapshot_dir=root / "snapshots",
                        environment_file=environment_path,
                        secrets_file=secrets_path,
                        chart_path=chart,
                        confirmed=True,
                        check_paths=False,
                        **self.backup_kwargs(root, catalog_path),
                    )
            self.assertFalse(any((root / "external-backups").glob("backup-*")))

    def test_rollback_does_not_touch_catalog_or_mr_without_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog_path = self.write_catalog(root)
            operations = ControlOperations()
            first = operations.deploy(catalog_path=catalog_path, snapshot_dir=root / "backups", confirmed=False, check_paths=False)
            original = catalog_path.read_text(encoding="utf-8")
            unconfirmed = operations.rollback(snapshot_path=first["snapshot"], catalog_path=catalog_path, confirmed=False)
            self.assertFalse(unconfirmed["applied"])
            self.assertEqual(catalog_path.read_text(encoding="utf-8"), original)

            replacement = root / "replacement.yaml"
            replacement.write_text("version: 1\nagents: {}\n", encoding="utf-8")
            chart = root / "openab-chart"
            chart.mkdir()
            (chart / "Chart.yaml").write_text("apiVersion: v2\nname: test-chart\nversion: 0.1.0\n", encoding="utf-8")
            environment_path = self.write_ready_environment(root, replacement)
            confirmed = operations.rollback(snapshot_path=first["snapshot"], catalog_path=replacement, confirmed=True, chart_path=chart, environment_file=environment_path, apply=lambda *_: "ok", k8s_apply=lambda *_: "k8s-ok")
            restored = replacement.read_text(encoding="utf-8")
        self.assertTrue(confirmed["confirmed"])
        self.assertIn("version: 1", restored)
        self.assertNotIn("agents: {}", restored)

    def test_confirmed_rollback_forwards_configured_deployer_kubeconfig(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog_path = self.write_catalog(root)
            operations = ControlOperations()
            snapshot = operations.deploy(
                catalog_path=catalog_path,
                snapshot_dir=root / "backups",
                confirmed=False,
                check_paths=False,
            )["snapshot"]
            replacement = root / "replacement.yaml"
            replacement.write_text("version: 1\nagents: {}\n", encoding="utf-8")
            chart = root / "openab-chart"
            chart.mkdir()
            (chart / "Chart.yaml").write_text("apiVersion: v2\nname: test-chart\nversion: 0.1.0\n", encoding="utf-8")
            environment_path = self.write_ready_environment(root, replacement)
            environment = yaml.safe_load(environment_path.read_text(encoding="utf-8"))
            environment["k3s"]["deployer_kubeconfig_env"] = "OAB_DEPLOYER_KUBECONFIG"
            environment_path.write_text(yaml.safe_dump(environment, sort_keys=False), encoding="utf-8")
            with patch.dict("oab_control.operations.os.environ", {"OAB_DEPLOYER_KUBECONFIG": "/secure/deployer-kubeconfig"}, clear=False):
                with patch("oab_control.operations._kubectl_apply", return_value="k8s-ok") as kubectl_apply:
                    with patch("oab_control.operations._helm_apply", return_value="helm-ok") as helm_apply:
                        result = operations.rollback(
                            snapshot_path=snapshot,
                            catalog_path=replacement,
                            confirmed=True,
                            environment_file=environment_path,
                            chart_path=chart,
                        )
        self.assertTrue(result["applied"])
        self.assertEqual(kubectl_apply.call_args.kwargs["kubeconfig"], "/secure/deployer-kubeconfig")
        self.assertEqual(helm_apply.call_args.kwargs["kubeconfig"], "/secure/deployer-kubeconfig")

    def test_failed_rollback_keeps_active_catalog_revision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog_path = self.write_catalog(root)
            operations = ControlOperations()
            first = operations.deploy(catalog_path=catalog_path, snapshot_dir=root / "backups", confirmed=False, check_paths=False)
            replacement = root / "replacement.yaml"
            replacement.write_text("version: 1\nagents: {}\n", encoding="utf-8")
            before = replacement.read_text(encoding="utf-8")
            chart = root / "openab-chart"
            chart.mkdir()
            environment_path = self.write_ready_environment(root, replacement)
            with self.assertRaises(RuntimeError):
                operations.rollback(
                    snapshot_path=first["snapshot"],
                    catalog_path=replacement,
                    confirmed=True,
                    chart_path=chart,
                    environment_file=environment_path,
                    apply=lambda *_: (_ for _ in ()).throw(RuntimeError("helm failed")),
                    k8s_apply=lambda *_: "k8s-ok",
                )
            after = replacement.read_text(encoding="utf-8")
        self.assertEqual(after, before)

    def test_confirmed_deploy_requires_chart_and_status_is_local_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog_path = self.write_catalog(root)
            with self.assertRaises(OperationError):
                ControlOperations().deploy(catalog_path=catalog_path, snapshot_dir=root / "backups", confirmed=True, check_paths=False)
            status = ControlOperations().status(catalog_path=catalog_path)
        self.assertEqual(status["runtime"]["state"], "unknown")
        self.assertEqual(status["namespace"], "oab-agents")

    def test_confirmed_deploy_requires_external_backup_before_any_apply(self) -> None:
        k8s_calls: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog_path = self.write_catalog(root)
            chart = root / "openab-chart"
            chart.mkdir()
            environment_path = self.write_ready_environment(root, catalog_path)
            with self.assertRaisesRegex(OperationError, "external backup"):
                ControlOperations().deploy(
                    catalog_path=catalog_path,
                    snapshot_dir=root / "snapshots",
                    environment_file=environment_path,
                    chart_path=chart,
                    confirmed=True,
                    check_paths=False,
                    k8s_apply=lambda manifest: k8s_calls.append(str(manifest)) or "should-not-run",
                )
            self.assertEqual(k8s_calls, [])
            self.assertFalse(any((root / "external-backups").glob("backup-*")))

    def test_default_helm_apply_requires_chart_metadata_before_backup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog_path = self.write_catalog(root)
            chart = root / "not-a-chart"
            chart.mkdir()
            environment_path = self.write_ready_environment(root, catalog_path)
            with patch.dict("oab_control.operations.os.environ", {"KUBECONFIG": "/secure/deployer-kubeconfig"}, clear=False):
                with self.assertRaisesRegex(OperationError, "Chart.yaml"):
                    ControlOperations().deploy(
                        catalog_path=catalog_path,
                        snapshot_dir=root / "snapshots",
                        environment_file=environment_path,
                        chart_path=chart,
                        confirmed=True,
                        check_paths=False,
                        k8s_apply=lambda _: "should-not-run",
                        **self.backup_kwargs(root, catalog_path),
                    )
            self.assertFalse(any((root / "external-backups").glob("backup-*")))

    def test_confirmed_deploy_rejects_catalog_outside_ready_environment_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog_path = self.write_catalog(root)
            chart = root / "openab-chart"
            chart.mkdir()
            environment_path = self.write_ready_environment(root, catalog_path)
            document = yaml.safe_load(environment_path.read_text(encoding="utf-8"))
            document["paths"]["agent_worktrees_root"] = str(root / "different-worktrees")
            (root / "different-worktrees").mkdir()
            environment_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
            with self.assertRaisesRegex(OperationError, "outside the environment worktree root"):
                ControlOperations().deploy(
                    catalog_path=catalog_path,
                    snapshot_dir=root / "snapshots",
                    environment_file=environment_path,
                    chart_path=chart,
                    confirmed=True,
                    check_paths=False,
                    **self.backup_kwargs(root, catalog_path),
                )

    def test_confirmed_deploy_rejects_backup_target_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog_path = self.write_catalog(root)
            chart = root / "openab-chart"
            chart.mkdir()
            environment_path = self.write_ready_environment(root, catalog_path)
            backup = self.backup_kwargs(root, catalog_path)
            backup["backup_output"] = root / "other-backups"
            with self.assertRaisesRegex(OperationError, "backup output"):
                ControlOperations().deploy(
                    catalog_path=catalog_path,
                    snapshot_dir=root / "snapshots",
                    environment_file=environment_path,
                    chart_path=chart,
                    confirmed=True,
                    check_paths=False,
                    **backup,
                )

    def test_confirmed_deploy_requires_materialized_worktrees_when_path_checks_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog_path = self.write_catalog(root)
            chart = root / "openab-chart"
            chart.mkdir()
            environment_path = self.write_ready_environment(root, catalog_path)
            with self.assertRaisesRegex(OperationError, "worktree is not materialized"):
                ControlOperations().deploy(
                    catalog_path=catalog_path,
                    snapshot_dir=root / "snapshots",
                    environment_file=environment_path,
                    chart_path=chart,
                    confirmed=True,
                    **self.backup_kwargs(root, catalog_path),
                )

    def test_status_uses_read_only_runtime_observer_when_available(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog_path = self.write_catalog(root)
            observed = []
            status = ControlOperations(
                namespace="oab-agents-test",
                runtime_observer=lambda namespace: observed.append(namespace) or {
                    "state": "healthy",
                    "source": "fixture",
                    "pods": [{"name": "developer", "phase": "Running", "ready": True, "restart_count": 0}],
                },
            ).status(catalog_path=catalog_path)
        self.assertEqual(observed, ["oab-agents-test"])
        self.assertEqual(status["runtime"]["state"], "healthy")
        self.assertEqual(status["runtime"]["source"], "fixture")

    def test_status_uses_ready_contract_kubeconfig_for_default_observer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog_path = self.write_catalog(root)
            environment_path = self.write_ready_environment(root, catalog_path)
            environment = yaml.safe_load(environment_path.read_text(encoding="utf-8"))
            environment["k3s"]["deployer_kubeconfig_env"] = "OAB_DEPLOYER_KUBECONFIG"
            environment_path.write_text(yaml.safe_dump(environment, sort_keys=False), encoding="utf-8")
            with patch.dict("oab_control.operations.os.environ", {"OAB_DEPLOYER_KUBECONFIG": "/secure/deployer-kubeconfig"}, clear=False):
                with patch("oab_control.operations._observe_kubernetes", return_value={"state": "healthy", "source": "fixture"}) as observer:
                    status = ControlOperations().status(catalog_path=catalog_path, environment_file=environment_path)
        self.assertEqual(status["runtime"]["state"], "healthy")
        self.assertEqual(observer.call_args.kwargs["kubeconfig"], "/secure/deployer-kubeconfig")

    def test_status_does_not_create_missing_task_store(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog_path = self.write_catalog(root)
            tasks_dir = root / "not-created"
            ControlOperations(runtime_observer=lambda _: {"state": "unknown"}).status(
                catalog_path=catalog_path,
                tasks_dir=tasks_dir,
            )
            self.assertFalse(tasks_dir.exists())

    def test_status_marks_missing_worktree_and_stale_task_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog_path = self.write_catalog(root)
            registry_json = root / "registry.json"
            WorkspaceRegistry(registry_json, root / "registry.md").save(
                [WorkspaceRecord(workspace_id="workspace-developer", owner_agent="developer", project_scope="service-x", worktree_path=str(root / "missing"))]
            )
            tasks_dir = root / "tasks"
            from test_tasks import task
            TaskStore(tasks_dir).create(task(), actor="leader")
            TaskStore(tasks_dir).transition("task-001", "assigned", actor="leader")
            TaskStore(tasks_dir).transition("task-001", "active", actor="leader")
            status = ControlOperations(runtime_observer=lambda _: {"state": "unknown"}).status(
                catalog_path=catalog_path,
                registry_json=registry_json,
                tasks_dir=tasks_dir,
            )
        self.assertEqual(status["workspace_observations"][0]["state"], "missing")
        self.assertEqual(status["task_observations"][0]["state"], "stale-checkpoint")

    def test_status_marks_active_task_for_reconciliation_when_catalog_routing_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document = catalog(root)
            catalog_path = root / "catalog.yaml"
            catalog_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
            from test_tasks import task
            developer = document["agents"]["developer"]
            grant = developer["repository_grants"][0]
            tasks_dir = root / "tasks"
            envelope = task().as_dict() | {
                "repository": grant["repository"],
                "checkout_subpath": grant["checkout_subpath"],
                "worktree_path": developer["worktree"]["path"],
                "container_mount_path": f"{developer['worktree']['container_mount_path']}/{grant['checkout_subpath']}",
                "base_branch": "origin/develop",
                "gitlab_identity_ref": developer["delivery"]["gitlab_identity_ref"],
                "reply_to": developer["discord"]["work_channel_id"],
                "deadline": "2099-08-31T23:59:00Z",
            }
            store = TaskStore(tasks_dir)
            store.create(Task(**envelope), actor="leader")
            store.transition("task-001", "assigned", actor="leader")
            document["agents"]["developer"]["discord"]["work_channel_id"] = "200000000000000099"
            catalog_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
            status = ControlOperations(runtime_observer=lambda _: {"state": "unknown"}).status(
                catalog_path=catalog_path,
                tasks_dir=tasks_dir,
            )
        observation = status["task_observations"][0]
        self.assertEqual(observation["state"], "needs-reconciliation")
        self.assertEqual(observation["catalog_binding"], "mismatch")
        self.assertIn("reply channel", observation["catalog_binding_reason"])


if __name__ == "__main__":
    unittest.main()
