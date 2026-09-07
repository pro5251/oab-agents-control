"""Local-first OpenAB agent control primitives."""

from .catalog import CatalogError, load_catalog, load_reference_manifest, validate_catalog
from .environment import EnvironmentDiagnostic, load_environment, load_environment_document, validate_environment
from .plan import render_plan
from .registry import RegistryError, WorkspaceRecord, WorkspaceRegistry
from .renderer import RenderError, render_agents_md, render_codex_config_toml, render_config_toml, render_openab_values, render_openab_values_yaml
from .operations import ControlOperations, OperationError, PreparedDeployment, SnapshotStore
from .k8s import KubernetesRenderError, agent_role_binding_name, agent_role_name, agent_service_account_name, catalog_revision, render_k8s_manifests, render_k8s_yaml
from .secrets import SecretError, SecretReference, load_secret_values, materialize_secrets, render_secret_manifests
from .tasks import Task, TaskError, TaskStore
from .worktree import Checkout, WorktreeError, WorktreeManager
from .backup import BackupError, BackupResult, LocalBackup
from .discord_policy import DiscordMessage, DiscordPolicyError, PolicyDecision, dispatch_channel, evaluate_message
from .assignments import AssignmentError, AssignmentStore, WorkAssignment
from .review_handoff import HandoffError, prepare_code_review
from .runtime_conformance import managed_codex_config_digest, observe_codex_features

__all__ = [
    "CatalogError",
    "EnvironmentDiagnostic",
    "load_catalog",
    "load_reference_manifest",
    "render_plan",
    "RegistryError",
    "WorkspaceRecord",
    "WorkspaceRegistry",
    "RenderError",
    "render_agents_md",
    "render_config_toml",
    "render_codex_config_toml",
    "render_openab_values",
    "render_openab_values_yaml",
    "ControlOperations",
    "OperationError",
    "PreparedDeployment",
    "SnapshotStore",
    "KubernetesRenderError",
    "agent_service_account_name",
    "agent_role_name",
    "agent_role_binding_name",
    "catalog_revision",
    "render_k8s_manifests",
    "render_k8s_yaml",
    "SecretError",
    "SecretReference",
    "load_secret_values",
    "materialize_secrets",
    "render_secret_manifests",
    "Task",
    "TaskError",
    "TaskStore",
    "Checkout",
    "WorktreeError",
    "WorktreeManager",
    "BackupError",
    "BackupResult",
    "LocalBackup",
    "load_environment",
    "load_environment_document",
    "validate_catalog",
    "validate_environment",
    "AssignmentError",
    "AssignmentStore",
    "WorkAssignment",
    "HandoffError",
    "prepare_code_review",
    "managed_codex_config_digest",
    "observe_codex_features",
]
