"""Guarded local control operations.

This module keeps side effects behind narrow seams.  Validation and rendering
are pure; snapshotting is local and auditable; Helm/Kubernetes is called only
after an explicit confirmation supplied by the operator.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any, Callable, Mapping
import uuid

from .catalog import Diagnostic, load_catalog, load_reference_manifest
from .environment import load_environment, load_environment_document
from .k8s import render_k8s_yaml
from .plan import render_plan
from .renderer import render_openab_values_yaml
from .registry import WorkspaceRegistry
from .secrets import SecretError, materialize_secrets
from .tasks import TaskError, TaskStore, validate_task_catalog_binding
from .yaml_utils import load_yaml
from .backup import BackupError, LocalBackup


class OperationError(RuntimeError):
    """Raised when an operator action cannot proceed safely."""


@dataclass(frozen=True)
class PreparedDeployment:
    catalog: dict[str, Any]
    plan: dict[str, Any]
    values_yaml: str


class SnapshotStore:
    """Create immutable catalog snapshots without copying Secret values."""

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def create(self, *, catalog_path: str | Path, plan: Mapping[str, Any]) -> Path:
        source = Path(catalog_path).resolve(strict=True)
        target_root = self.root.resolve(strict=False)
        if target_root == source:
            raise OperationError("snapshot target must not be the catalog file")
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.root / f"snapshot-{_timestamp()}-{uuid.uuid4().hex[:8]}"
        target.mkdir()
        shutil.copy2(source, target / "catalog.yaml")
        _atomic_text(target / "plan.json", json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        _atomic_text(
            target / "metadata.json",
            json.dumps(
                {
                    "version": 1,
                    "created_at": _now(),
                    "source_catalog": str(source),
                    "secret_values_included": False,
                    "k3s_apply_performed": False,
                    "helm_apply_performed": False,
                    "secret_materialization_performed": False,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )
        return target

    def list(self) -> list[Path]:
        if not self.root.exists():
            return []
        return sorted((path for path in self.root.iterdir() if path.is_dir() and path.name.startswith("snapshot-")), reverse=True)


class ControlOperations:
    """Coordinate validation, plans, snapshots, and explicitly confirmed apply."""

    def __init__(self, *, namespace: str = "oab-agents", runtime_observer: Callable[[str], dict[str, Any]] | None = None):
        if not namespace or "/" in namespace or namespace in {".", ".."}:
            raise OperationError("unsafe Kubernetes namespace")
        self.namespace = namespace
        self.runtime_observer = runtime_observer

    def prepare(
        self,
        *,
        catalog_path: str | Path,
        secrets_file: str | Path | None = None,
        check_paths: bool = True,
        check_git: bool = False,
    ) -> PreparedDeployment:
        secret_refs: set[str] | None = None
        identity_refs: set[str] | None = None
        if secrets_file:
            secret_refs, identity_refs, reference_errors = load_reference_manifest(secrets_file)
            if reference_errors:
                raise OperationError(_format_diagnostics(reference_errors))
        catalog, diagnostics = load_catalog(
            catalog_path,
            check_paths=check_paths,
            check_git=check_git,
            available_secret_refs=secret_refs,
            available_identity_refs=identity_refs,
        )
        if diagnostics or catalog is None:
            raise OperationError(_format_diagnostics(diagnostics))
        return PreparedDeployment(
            catalog=catalog,
            plan=render_plan(catalog, namespace=self.namespace),
            values_yaml=render_openab_values_yaml(catalog),
        )

    def deploy(
        self,
        *,
        catalog_path: str | Path,
        snapshot_dir: str | Path,
        environment_file: str | Path | None = None,
        secrets_file: str | Path | None = None,
        chart_path: str | Path | None = None,
        release: str = "oab-agents",
        confirmed: bool,
        check_paths: bool = True,
        check_git: bool = False,
        apply: Callable[[Path, Path, str, str], str] | None = None,
        k8s_apply: Callable[[Path], str] | None = None,
        secret_apply: Callable[[list[dict[str, Any]]], str] | None = None,
        backup_sources: Mapping[str, str | Path] | None = None,
        backup_output: str | Path | None = None,
        backup_attestation: str | None = None,
    ) -> dict[str, Any]:
        environment_document: Mapping[str, Any] | None = None
        deployer_kubeconfig: str | None = None
        secret_materializer_kubeconfig: str | None = None
        materialize_local_secrets = bool(secrets_file and _has_secret_values_file(secrets_file))
        if confirmed:
            if environment_file is None:
                raise OperationError("confirmed deploy requires --environment pointing to a ready contract")
            environment_document, environment_load_errors = load_environment_document(environment_file)
            if environment_load_errors:
                raise OperationError("unable to load environment contract")
            environment_errors = load_environment(environment_file, require_ready=True)
            if environment_errors:
                raise OperationError("environment contract is not ready: " + "; ".join(f"{item.path}: {item.code}" for item in environment_errors))
            if chart_path is None:
                raise OperationError("confirmed deploy requires an explicit OpenAB chart path")
            chart = Path(chart_path).resolve(strict=True)
            if not chart.is_dir():
                raise OperationError("OpenAB chart path must be a directory")
        prepared = self.prepare(
            catalog_path=catalog_path,
            secrets_file=secrets_file,
            check_paths=check_paths,
            check_git=check_git,
        )
        if confirmed:
            self._validate_environment_alignment(
                prepared.catalog,
                environment_document,
                catalog_path=catalog_path,
                backup_sources=backup_sources,
                backup_output=backup_output,
            )
            if check_paths:
                self._validate_materialized_worktrees(prepared.catalog)
            if backup_sources is None or backup_output is None or backup_attestation is None:
                raise OperationError("confirmed deploy requires external backup sources, output, and encryption attestation")
            if apply is None:
                _require_helm_chart(chart)
            if k8s_apply is None or apply is None:
                deployer_kubeconfig = self._require_deployer_kubeconfig(environment_document)
            if materialize_local_secrets and secret_apply is None:
                secret_materializer_kubeconfig = self._require_secret_materializer_kubeconfig(environment_document)
                if deployer_kubeconfig is not None and secret_materializer_kubeconfig == deployer_kubeconfig:
                    raise OperationError("Secret materializer kubeconfig must differ from the deployer kubeconfig")
        backup_result = None
        if confirmed:
            if backup_sources is None or backup_output is None or backup_attestation is None:
                raise OperationError("confirmed deploy requires external backup sources, output, and encryption attestation")
            try:
                backup_result = LocalBackup(backup_output).create(backup_sources, encryption_attestation=backup_attestation)
            except (BackupError, OSError) as exc:
                raise OperationError(f"external backup failed; deployment was not attempted: {exc}") from exc
        snapshot = SnapshotStore(snapshot_dir).create(catalog_path=catalog_path, plan=prepared.plan)
        values_path = snapshot / "openab-values.yaml"
        _atomic_text(values_path, prepared.values_yaml)
        k8s_path = snapshot / "k8s-isolation.yaml"
        _atomic_text(
            k8s_path,
            # Namespace and RBAC are bootstrap-only: the deployer identity has
            # no permission for them, so a confirmed apply must render only the
            # subset it is actually scoped to manage.
            render_k8s_yaml(
                prepared.catalog,
                namespace=self.namespace,
                deployer_scoped=True,
                egress_mode=_egress_mode(environment_document),
            ),
        )
        result: dict[str, Any] = {
            "namespace": self.namespace,
            "snapshot": str(snapshot),
            "plan": prepared.plan,
            "confirmed": confirmed,
            "applied": False,
        }
        if confirmed:
            result["external_backup"] = backup_result.as_dict()
        if not confirmed:
            result["reason"] = "human confirmation required; Kubernetes was not modified"
            return result
        assert chart is not None
        apply_result = apply or (
            lambda chart, values, release_name, namespace: _helm_apply(
                chart,
                values,
                release_name,
                namespace,
                kubeconfig=deployer_kubeconfig,
            )
        )
        if materialize_local_secrets:
            secret_refs = sorted({
                agent["discord"]["bot_secret_ref"]
                for agent in prepared.catalog["agents"].values()
            })
            try:
                result["secret_apply_output"] = materialize_secrets(
                    secret_refs,
                    values_file=secrets_file,
                    namespace=self.namespace,
                    apply=secret_apply
                    or (lambda manifests: _kubectl_apply_secrets(manifests, kubeconfig=secret_materializer_kubeconfig)),
                )
                _mark_snapshot_field(snapshot, "secret_materialization_performed", True)
            except Exception as exc:
                _mark_snapshot_failure(snapshot, exc)
                if isinstance(exc, SecretError):
                    raise OperationError(str(exc)) from exc
                raise OperationError(f"Secret materialization failed after snapshot: {type(exc).__name__}") from exc
        try:
            k8s_apply_result = (k8s_apply or (lambda manifest: _kubectl_apply(manifest, kubeconfig=deployer_kubeconfig)))(k8s_path)
            result["k8s_apply_output"] = k8s_apply_result
            _mark_snapshot_field(snapshot, "k3s_apply_performed", True)
            result["apply_output"] = apply_result(chart, values_path, release, self.namespace)
            _mark_snapshot_field(snapshot, "helm_apply_performed", True)
        except Exception as exc:
            _mark_snapshot_failure(snapshot, exc)
            if isinstance(exc, OperationError):
                raise
            raise OperationError(f"confirmed deploy failed after snapshot: {type(exc).__name__}") from exc
        result["applied"] = True
        _mark_snapshot_applied(snapshot)
        return result

    @staticmethod
    def _validate_environment_alignment(
        catalog: Mapping[str, Any],
        environment: Mapping[str, Any] | None,
        *,
        catalog_path: str | Path,
        backup_sources: Mapping[str, str | Path] | None,
        backup_output: str | Path | None,
    ) -> None:
        """Keep a ready environment and catalog on the same host boundaries."""

        if not isinstance(environment, Mapping):
            raise OperationError("ready environment contract is not a mapping")
        paths = environment.get("paths")
        if not isinstance(paths, Mapping):
            raise OperationError("ready environment contract has no paths mapping")
        try:
            expected_catalog = Path(str(paths["catalog"])).resolve(strict=False)
            actual_catalog = Path(catalog_path).resolve(strict=False)
            collection_roots = [Path(str(item)).resolve(strict=False) for item in paths["collection_roots"]]
            worktrees_root = Path(str(paths["agent_worktrees_root"])).resolve(strict=False)
            expected_backup = {
                "catalog": expected_catalog,
                "coordination_repository": Path(str(paths["coordination_repository"])).resolve(strict=False),
                "agent_worktrees_root": worktrees_root,
                "k3s_state": Path(str(paths["k3s_state"])).resolve(strict=False),
            }
        except (KeyError, TypeError, ValueError) as exc:
            raise OperationError("ready environment contract has invalid path mappings") from exc
        if actual_catalog != expected_catalog:
            raise OperationError("catalog path does not match the ready environment contract")
        for agent_id, agent in catalog["agents"].items():
            worktree = Path(agent["worktree"]["path"]).resolve(strict=False)
            if not worktree.is_relative_to(worktrees_root) or worktree == worktrees_root:
                raise OperationError(f"agent worktree is outside the environment worktree root: {agent_id}")
            for grant in agent["repository_grants"]:
                repository = Path(grant["repository"]).resolve(strict=False)
                if not any(repository.is_relative_to(root) and repository != root for root in collection_roots):
                    raise OperationError(f"repository grant is outside the environment collection roots: {repository}")
        if backup_sources is not None:
            for component, expected in expected_backup.items():
                actual = Path(str(backup_sources.get(component, ""))).resolve(strict=False)
                if actual != expected:
                    raise OperationError(f"backup source does not match the environment contract: {component}")
        if backup_output is not None:
            expected_target = Path(str(paths["backup_target"])).resolve(strict=False)
            actual_target = Path(str(backup_output)).resolve(strict=False)
            if actual_target != expected_target:
                raise OperationError("backup output does not match the ready environment backup target")

    @staticmethod
    def _validate_materialized_worktrees(catalog: Mapping[str, Any]) -> None:
        """Require exact checkout directories before rendering Directory hostPath mounts."""

        for agent_id, agent in catalog["agents"].items():
            worktree_root = Path(agent["worktree"]["path"]).resolve(strict=False)
            if not worktree_root.is_dir():
                raise OperationError(f"agent worktree is not materialized: {agent_id}")
            for grant in agent["repository_grants"]:
                checkout = (worktree_root / grant["checkout_subpath"]).resolve(strict=False)
                if not checkout.is_dir() or not checkout.is_relative_to(worktree_root) or not (checkout / ".git").exists():
                    raise OperationError(f"agent checkout is not materialized: {agent_id}/{grant['checkout_subpath']}")

    @staticmethod
    def _require_deployer_kubeconfig(environment: Mapping[str, Any] | None) -> str:
        return ControlOperations._require_kubeconfig_environment(
            environment,
            field="deployer_kubeconfig_env",
            description="namespace-scoped deployer",
        )

    @staticmethod
    def _require_secret_materializer_kubeconfig(environment: Mapping[str, Any] | None) -> str:
        return ControlOperations._require_kubeconfig_environment(
            environment,
            field="secret_materializer_kubeconfig_env",
            description="Secret materializer",
        )

    @staticmethod
    def _require_kubeconfig_environment(
        environment: Mapping[str, Any] | None,
        *,
        field: str,
        description: str,
    ) -> str:
        k3s = environment.get("k3s") if isinstance(environment, Mapping) else None
        variable = k3s.get(field) if isinstance(k3s, Mapping) else None
        value = os.environ.get(variable) if isinstance(variable, str) else None
        if not isinstance(variable, str) or not variable or not value:
            raise OperationError(f"confirmed deploy requires the configured {description} kubeconfig environment variable")
        return value

    def rollback(
        self,
        *,
        snapshot_path: str | Path,
        catalog_path: str | Path,
        confirmed: bool,
        environment_file: str | Path | None = None,
        chart_path: str | Path | None = None,
        release: str = "oab-agents",
        apply: Callable[[Path, Path, str, str], str] | None = None,
        k8s_apply: Callable[[Path], str] | None = None,
    ) -> dict[str, Any]:
        environment_document: Mapping[str, Any] | None = None
        deployer_kubeconfig: str | None = None
        if confirmed:
            if environment_file is None:
                raise OperationError("confirmed rollback requires --environment pointing to a ready contract")
            environment_document, environment_load_errors = load_environment_document(environment_file)
            if environment_load_errors:
                raise OperationError("unable to load environment contract")
            environment_errors = load_environment(environment_file, require_ready=True)
            if environment_errors:
                raise OperationError("environment contract is not ready: " + "; ".join(f"{item.path}: {item.code}" for item in environment_errors))
        snapshot = Path(snapshot_path).resolve(strict=True)
        previous_catalog = snapshot / "catalog.yaml"
        if not previous_catalog.is_file():
            raise OperationError("snapshot has no catalog.yaml")
        prepared = self.prepare(catalog_path=previous_catalog, check_paths=False)
        if confirmed:
            self._validate_environment_alignment(
                prepared.catalog,
                environment_document,
                catalog_path=catalog_path,
                backup_sources=None,
                backup_output=None,
            )
        result: dict[str, Any] = {
            "snapshot": str(snapshot),
            "catalog": str(catalog_path),
            "plan": prepared.plan,
            "confirmed": confirmed,
            "applied": False,
            "worktrees_or_gitlab_modified": False,
        }
        if not confirmed:
            result["reason"] = "human confirmation required; catalog and Kubernetes were not modified"
            return result
        if chart_path is None:
            raise OperationError("confirmed rollback requires an explicit OpenAB chart path")
        target = Path(catalog_path).resolve(strict=False)
        if target == previous_catalog:
            raise OperationError("rollback target must be different from snapshot catalog")
        chart = Path(chart_path).resolve(strict=True)
        if not chart.is_dir():
            raise OperationError("OpenAB chart path must be a directory")
        if apply is None:
            _require_helm_chart(chart)
        if k8s_apply is None or apply is None:
            deployer_kubeconfig = self._require_deployer_kubeconfig(environment_document)
        apply_result = apply or (
            lambda chart, values, release_name, namespace: _helm_apply(
                chart,
                values,
                release_name,
                namespace,
                kubeconfig=deployer_kubeconfig,
            )
        )
        values_path = snapshot / "rollback-openab-values.yaml"
        _atomic_text(values_path, prepared.values_yaml)
        k8s_path = snapshot / "rollback-k8s-isolation.yaml"
        _atomic_text(
            k8s_path,
            # Namespace and RBAC are bootstrap-only: the deployer identity has
            # no permission for them, so a confirmed apply must render only the
            # subset it is actually scoped to manage.
            render_k8s_yaml(
                prepared.catalog,
                namespace=self.namespace,
                deployer_scoped=True,
                egress_mode=_egress_mode(environment_document),
            ),
        )
        result["k8s_apply_output"] = (k8s_apply or (lambda manifest: _kubectl_apply(manifest, kubeconfig=deployer_kubeconfig)))(k8s_path)
        result["apply_output"] = apply_result(chart, values_path, release, self.namespace)
        # Keep the active catalog unchanged until both desired-state applies
        # succeed.  A failed Helm/Kubernetes operation therefore cannot leave
        # the local source claiming a revision that was never deployed.
        _atomic_text(target, previous_catalog.read_text(encoding="utf-8"))
        result["applied"] = True
        return result

    def status(
        self,
        *,
        catalog_path: str | Path,
        environment_file: str | Path | None = None,
        registry_json: str | Path | None = None,
        registry_markdown: str | Path | None = None,
        tasks_dir: str | Path | None = None,
        secrets_file: str | Path | None = None,
    ) -> dict[str, Any]:
        prepared = self.prepare(catalog_path=catalog_path, secrets_file=secrets_file, check_paths=False)
        status: dict[str, Any] = {"namespace": self.namespace, "plan": prepared.plan, "workspaces": [], "tasks": []}
        if registry_json:
            registry = WorkspaceRegistry(registry_json, registry_markdown or Path(registry_json).with_suffix(".md"))
            records = registry.load()
            status["workspaces"] = [record.as_dict() for record in records]
            status["workspace_observations"] = [_observe_workspace(record) for record in records]
        if tasks_dir:
            tasks = TaskStore(tasks_dir).list()
            status["tasks"] = [task.as_dict() for task in tasks]
            status["task_observations"] = [_observe_task(task, prepared.catalog) for task in tasks]
        kubeconfig: str | None = None
        if environment_file is not None:
            environment_document, environment_load_errors = load_environment_document(environment_file)
            if environment_load_errors:
                raise OperationError("unable to load environment contract")
            environment_errors = load_environment(environment_file)
            if environment_errors:
                raise OperationError("environment contract is invalid: " + "; ".join(f"{item.path}: {item.code}" for item in environment_errors))
            if isinstance(environment_document, Mapping) and environment_document.get("status") == "ready":
                kubeconfig = self._require_deployer_kubeconfig(environment_document)
        observer = self.runtime_observer or (lambda namespace: _observe_kubernetes(namespace, kubeconfig=kubeconfig))
        try:
            status["runtime"] = observer(self.namespace)
        except (OSError, OperationError, ValueError) as exc:
            # Status is read-only and must remain useful on a bootstrap host;
            # an unavailable observer is a visible unknown, never an invented
            # healthy state.
            status["runtime"] = {"state": "unknown", "reason": f"K3s observation unavailable: {type(exc).__name__}"}
        return status


def _helm_apply(chart: Path, values: Path, release: str, namespace: str, *, kubeconfig: str | None = None) -> str:
    # Helm 3 stores release state as Secrets by default, but the rendered
    # ``oab-control-deployer`` Role deliberately has no secrets permission at
    # all -- reading Secret values is reserved for the separate materializer
    # identity.  Granting the deployer secrets access to satisfy Helm would
    # hand it every Secret in the namespace, so the release driver is switched
    # to ConfigMaps, which the deployer Role already covers.
    environment = _control_env(kubeconfig=kubeconfig) | {"HELM_DRIVER": "configmap"}
    try:
        process = subprocess.run(
            # Namespace creation is rendered/applied by the namespace-scoped
            # isolation manifest; Helm must not require a cluster-wide
            # create-namespace permission from the deployer identity.
            ["helm", "upgrade", "--install", release, str(chart), "--namespace", namespace, "--values", str(values)],
            check=True,
            capture_output=True,
            text=True,
            timeout=300,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        raise OperationError(f"Helm apply failed: {type(exc).__name__}: {detail[:400]}") from exc
    return process.stdout[-400:]


def _require_helm_chart(chart: Path) -> None:
    """Reject a directory that cannot be used by the default Helm apply seam."""

    if not (chart / "Chart.yaml").is_file():
        raise OperationError("OpenAB chart path must contain Chart.yaml when using the default Helm apply")


def _kubectl_apply(manifest: Path, *, kubeconfig: str | None = None) -> str:
    try:
        process = subprocess.run(
            ["kubectl", "apply", "--filename", str(manifest)],
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
            env=_control_env(kubeconfig=kubeconfig),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        raise OperationError(f"Kubernetes isolation apply failed: {type(exc).__name__}: {detail[:400]}") from exc
    return process.stdout[-400:]


def _kubectl_apply_secrets(manifests: list[dict[str, Any]], *, kubeconfig: str | None = None) -> str:
    import yaml

    payload = "\n---\n".join(yaml.safe_dump(item, allow_unicode=True, sort_keys=False).rstrip() for item in manifests) + "\n"
    try:
        process = subprocess.run(
            # Server-side apply uses create/patch semantics and does not need
            # a preliminary GET of the Secret value.  This matches the
            # materializer Role, which intentionally has no secrets/get.
            ["kubectl", "apply", "--server-side", "--field-manager=oab-control-secret-materializer", "--filename", "-"],
            input=payload,
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
            env=_control_env(kubeconfig=kubeconfig),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        raise OperationError(f"Kubernetes Secret apply failed: {type(exc).__name__}: {detail[:400]}") from exc
    return process.stdout[-400:]


def _observe_kubernetes(namespace: str, *, kubeconfig: str | None = None) -> dict[str, Any]:
    """Read pod health through kubectl without requesting Secret contents."""

    try:
        process = subprocess.run(
            ["kubectl", "get", "pods", "--namespace", namespace, "--output", "json"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
            env=_control_env(kubeconfig=kubeconfig),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        raise OperationError(f"kubectl pod observation failed: {_redact(detail).strip()[:240] or type(exc).__name__}") from exc
    try:
        document = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise OperationError("kubectl pod observation returned invalid JSON") from exc
    if not isinstance(document, Mapping) or not isinstance(document.get("items"), list):
        raise OperationError("kubectl pod observation returned an unexpected document")
    pods: list[dict[str, Any]] = []
    for item in document["items"]:
        if not isinstance(item, Mapping):
            continue
        metadata = item.get("metadata") if isinstance(item.get("metadata"), Mapping) else {}
        spec = item.get("status") if isinstance(item.get("status"), Mapping) else {}
        labels = metadata.get("labels") if isinstance(metadata.get("labels"), Mapping) else {}
        containers = spec.get("containerStatuses") if isinstance(spec.get("containerStatuses"), list) else []
        ready = bool(containers) and all(isinstance(container, Mapping) and container.get("ready") is True for container in containers)
        restarts = sum(int(container.get("restartCount", 0)) for container in containers if isinstance(container, Mapping) and isinstance(container.get("restartCount", 0), int))
        pods.append(
            {
                "name": str(metadata.get("name", "")),
                "agent_id": str(labels.get("app.kubernetes.io/component", "")),
                "phase": str(spec.get("phase", "Unknown")),
                "ready": ready,
                "restart_count": restarts,
            }
        )
    pods.sort(key=lambda pod: pod["name"])
    if not pods:
        state = "unknown"
    elif all(pod["phase"] == "Running" and pod["ready"] for pod in pods):
        state = "healthy"
    elif any(pod["phase"] in {"Failed", "Unknown"} for pod in pods):
        state = "degraded"
    else:
        state = "starting"
    return {"state": state, "source": "kubectl", "namespace": namespace, "pods": pods}


#: Files a coding CLI treats as instructions.  Inside a granted checkout they
#: are project content -- which is to say, attacker-reachable content -- so
#: their presence is worth surfacing even though nothing can stop an agent
#: reading them.  See docs/規格-agent-工作流程.md section 4.
_INSTRUCTION_FILENAMES = frozenset(
    {"AGENTS.md", "CLAUDE.md", "GEMINI.md", ".cursorrules", "copilot-instructions.md"}
)


def _instruction_files(checkout: Path) -> list[str]:
    """Name instruction files a checkout carries, relative to that checkout.

    Only the checkout root and one level below: deeper files are not picked up
    by the CLIs this guards against, and walking a large repository on every
    status call is not worth the cost.
    """

    found: list[str] = []
    try:
        candidates = list(checkout.iterdir())
        for directory in (item for item in candidates if item.is_dir() and not item.name.startswith(".")):
            try:
                candidates.extend(directory.iterdir())
            except OSError:
                continue
    except OSError:
        return found
    for item in candidates:
        try:
            if item.is_file() and item.name in _INSTRUCTION_FILENAMES:
                found.append(str(item.relative_to(checkout)))
        except (OSError, ValueError):
            continue
    return sorted(found)


def _observe_workspace(record: Any) -> dict[str, Any]:
    """Inspect only registered worktree metadata and Git status."""

    root = Path(record.worktree_path).resolve(strict=False)
    if not root.is_dir():
        return {"workspace_id": record.workspace_id, "state": "missing", "path": str(root), "checkouts": []}
    checkouts: list[dict[str, Any]] = []
    try:
        git_metadata_paths = sorted(root.rglob(".git"))
    except OSError:
        return {"workspace_id": record.workspace_id, "state": "unavailable", "path": str(root), "checkouts": []}
    for git_metadata in git_metadata_paths:
        checkout = git_metadata.parent
        if not checkout.is_dir():
            continue
        try:
            branch = _git_read(["-C", str(checkout), "branch", "--show-current"])
            dirty = bool(_git_read(["-C", str(checkout), "status", "--porcelain"]))
        except OperationError:
            checkouts.append({"path": str(checkout), "state": "unavailable"})
        else:
            observation = {"path": str(checkout), "branch": branch, "dirty": dirty, "state": "dirty" if dirty else "clean"}
            instructions = _instruction_files(checkout)
            if instructions:
                observation["instruction_files"] = instructions
            checkouts.append(observation)
    flagged = sorted({name for item in checkouts for name in item.get("instruction_files", [])})
    if not checkouts:
        state = "empty"
    elif any(item.get("state") == "unavailable" for item in checkouts):
        state = "unavailable"
    elif any(item.get("dirty") for item in checkouts):
        state = "dirty"
    else:
        state = "clean"
    result = {"workspace_id": record.workspace_id, "state": state, "path": str(root), "checkouts": checkouts}
    if flagged:
        # Not an error: the operator decides.  But an agent that reads one of
        # these is taking instructions from project content, which the spec
        # says it must treat as data.
        result["instruction_files_present"] = flagged
    return result


def _observe_task(task: Any, catalog: Mapping[str, Any]) -> dict[str, Any]:
    """Observe task timing and whether its durable envelope still has a grant."""

    # A closed task's binding is history: the catalog may have moved on, but
    # there is nothing left to reconcile.  Flagging it forever turns the signal
    # into noise that accumulates with every finished task.
    terminal = task.state == "closed"

    binding_error: str | None = None
    try:
        validate_task_catalog_binding(task, catalog)
    except TaskError as exc:
        # The catalog is the authorization source.  Never expose task content
        # in status, but make a routing/grant change visible to the operator.
        binding_error = str(exc)
    if task.state == "needs-reconciliation":
        observation = {"task_id": task.task_id, "state": "needs-reconciliation", "action": "manual reconciliation required", "heartbeat": "not-configured"}
    else:
        try:
            deadline = datetime.fromisoformat(task.deadline.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            observation = {"task_id": task.task_id, "state": "invalid-deadline", "heartbeat": "not-configured"}
        else:
            now = datetime.now(timezone.utc)
            if task.state in {"assigned", "active", "review"} and deadline <= now:
                observation = {"task_id": task.task_id, "state": "stale-checkpoint", "action": "manual reconciliation required", "heartbeat": "not-configured"}
            else:
                observation = {"task_id": task.task_id, "state": "on-track", "heartbeat": "not-configured"}
    if binding_error is not None and terminal:
        observation["catalog_binding"] = "stale"
        observation["catalog_binding_reason"] = binding_error
    elif binding_error is not None:
        observation["catalog_binding"] = "mismatch"
        observation["catalog_binding_reason"] = binding_error
        if observation["state"] == "on-track":
            observation["state"] = "needs-reconciliation"
            observation["action"] = "catalog routing or repository grant changed; manual reconciliation required"
    else:
        observation["catalog_binding"] = "aligned"
    return observation


def _git_read(args: list[str]) -> str:
    try:
        process = subprocess.run(
            ["git", *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
            env=_control_env(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise OperationError(f"Git observation failed: {type(exc).__name__}") from exc
    return process.stdout.strip()


def _redact(value: str) -> str:
    import re

    value = re.sub(r"((?:https?|ssh)://)([^/@]+):([^/@]+)@", r"\1<redacted>@", value)
    return re.sub(r"(?:glpat-|gh[pousr]_|sk-|xox[baprs]-)[A-Za-z0-9_-]+", "<redacted>", value)


def _has_secret_values_file(path: str | Path) -> bool:
    try:
        import yaml

        with Path(path).open("r", encoding="utf-8") as stream:
            document = load_yaml(stream)
    except (OSError, yaml.YAMLError):
        return False
    return isinstance(document, Mapping) and isinstance(document.get("secrets"), Mapping)


def _control_env(*, kubeconfig: str | None = None) -> dict[str, str]:
    environment = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")}
    selected_kubeconfig = kubeconfig or os.environ.get("KUBECONFIG")
    if selected_kubeconfig:
        environment["KUBECONFIG"] = selected_kubeconfig
    return environment


def _mark_snapshot_applied(snapshot: Path) -> None:
    metadata_path = snapshot / "metadata.json"
    document = json.loads(metadata_path.read_text(encoding="utf-8"))
    document["k3s_apply_performed"] = True
    document["helm_apply_performed"] = True
    document["applied_at"] = _now()
    _atomic_text(metadata_path, json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _mark_snapshot_field(snapshot: Path, field: str, value: bool) -> None:
    """Persist a non-secret side-effect marker after each apply phase."""

    if field not in {"k3s_apply_performed", "helm_apply_performed", "secret_materialization_performed"}:
        raise OperationError("invalid snapshot phase marker")
    metadata_path = snapshot / "metadata.json"
    document = json.loads(metadata_path.read_text(encoding="utf-8"))
    document[field] = value
    _atomic_text(metadata_path, json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _mark_snapshot_failure(snapshot: Path, error: Exception) -> None:
    metadata_path = snapshot / "metadata.json"
    try:
        document = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    document["apply_failed"] = True
    document["apply_failure_type"] = type(error).__name__
    _atomic_text(metadata_path, json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _egress_mode(environment: Mapping[str, Any] | None) -> str:
    """Read the operator's recorded egress decision, defaulting to strict.

    An absent contract (an unconfirmed preview) renders the strict default, so
    a preview never shows a wider policy than a confirmed apply would install.
    """

    k3s = environment.get("k3s") if isinstance(environment, Mapping) else None
    mode = k3s.get("egress_mode") if isinstance(k3s, Mapping) else None
    return mode if isinstance(mode, str) and mode else "proxy-only"


def _format_diagnostics(diagnostics: list[Diagnostic]) -> str:
    return "; ".join(f"{diagnostic.path}: {diagnostic.code}" for diagnostic in diagnostics) or "catalog validation failed"


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise OperationError(f"unable to persist operation state: {type(exc).__name__}") from exc
