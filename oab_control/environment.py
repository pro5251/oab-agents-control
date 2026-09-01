"""Validation for the non-secret local bootstrap contract.

This file intentionally accepts a ``bootstrap-pending`` contract with
operator-owned pending decisions.  It prevents guessed Discord/GitLab values
from being mistaken for a deployable environment while giving later catalog
validation one stable source of paths.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Mapping

import yaml

from .yaml_utils import load_yaml


@dataclass(frozen=True)
class EnvironmentDiagnostic:
    path: str
    code: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"path": self.path, "code": self.code, "message": self.message}


_ABSOLUTE = re.compile(r"^/[^\0]*$")
_SECRET_VALUE = re.compile(r"(?:sk-|glpat-|gh[pousr]_|xox[baprs]-|(?:https?|ssh)://[^/\s:@]+:[^@\s]+@|-----BEGIN [A-Z ]*PRIVATE KEY-----|(?:token|password|secret)\s*[:=])", re.I)
_ENV_VAR = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_PATH_FIELDS = {
    "catalog",
    "coordination_repository",
    "collection_roots",
    "agent_worktrees_root",
    "k3s_state",
    "backup_target",
    "secrets_file",
}


def validate_environment(document: Any, *, require_ready: bool = False) -> list[EnvironmentDiagnostic]:
    errors: list[EnvironmentDiagnostic] = []

    def error(path: str, code: str, message: str) -> None:
        errors.append(EnvironmentDiagnostic(path, code, message))

    def mapping(value: Any, path: str) -> Mapping[str, Any] | None:
        if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
            error(path, "type", "expected a mapping with string keys")
            return None
        return value

    root = mapping(document, "$")
    if root is None:
        return errors
    allowed = {"version", "status", "implementation", "paths", "gitlab", "discord", "profiles", "k3s", "pending_decisions"}
    for key in sorted(set(root) - allowed):
        error(f"$.{key}", "unknown_field", "field is not part of the environment contract")
    if root.get("version") != 1:
        error("$.version", "version", "only environment contract version 1 is supported")
    status = root.get("status")
    if status not in {"bootstrap-pending", "ready"}:
        error("$.status", "status", "must be bootstrap-pending or ready")
    pending = root.get("pending_decisions", [])
    if not isinstance(pending, list):
        error("$.pending_decisions", "type", "expected a list")
        pending = []
    if require_ready and (status != "ready" or pending):
        error("$.status", "not_ready", "deployment requires status=ready and no pending decisions")

    paths = mapping(root.get("paths"), "$.paths")
    if paths is None:
        paths = {}
    for key in sorted(set(paths) - _PATH_FIELDS):
        error(f"$.paths.{key}", "unknown_field", "field is not part of the environment contract")
    for key in sorted(_PATH_FIELDS - set(paths)):
        error(f"$.paths.{key}", "required", "path is required")
    for key, value in paths.items():
        if key not in _PATH_FIELDS:
            continue
        if key == "collection_roots":
            if not isinstance(value, list) or not value:
                error(f"$.paths.{key}", "type", "must be a non-empty list of absolute paths")
                continue
            values = value
        else:
            values = [value]
        for index, candidate in enumerate(values):
            path = f"$.paths.{key}[{index}]" if key == "collection_roots" else f"$.paths.{key}"
            if not isinstance(candidate, str) or not _ABSOLUTE.fullmatch(candidate) or any(part in {".", ".."} for part in Path(candidate).parts):
                error(path, "absolute_path", "must be an explicit absolute POSIX path")
            elif key != "secrets_file" and status == "ready" and not Path(candidate).exists():
                error(path, "missing_path", "path does not exist")

    implementation = mapping(root.get("implementation"), "$.implementation")
    if implementation is None:
        implementation = {}
    for key in sorted(set(implementation) - {"repository", "default_branch"}):
        error(f"$.implementation.{key}", "unknown_field", "field is not part of the environment contract")
    for key in ("repository", "default_branch"):
        if key not in implementation:
            error(f"$.implementation.{key}", "required", "field is required")
    repository = implementation.get("repository")
    if repository is not None and (not isinstance(repository, str) or not _ABSOLUTE.fullmatch(repository) or any(part in {".", ".."} for part in Path(repository).parts)):
        error("$.implementation.repository", "absolute_path", "implementation repository must be an explicit absolute POSIX path")
    elif isinstance(repository, str) and status == "ready" and not Path(repository).exists():
        error("$.implementation.repository", "missing_path", "implementation repository does not exist")
    if implementation.get("default_branch") != "main":
        error("$.implementation.default_branch", "branch", "local control repository defaults to main")

    k3s = mapping(root.get("k3s"), "$.k3s")
    if k3s is None:
        if require_ready:
            error("$.k3s", "required", "ready deployment contracts must record K3s identity, encryption recovery, and network-policy controller")
        k3s = {}
    else:
        for key in sorted(set(k3s) - {"context", "deployer_kubeconfig_env", "secret_materializer_kubeconfig_env", "secrets_encryption_enabled", "secrets_encryption_recovery_ref", "network_policy_controller"}):
            error(f"$.k3s.{key}", "unknown_field", "field is not part of the K3s bootstrap contract")
        for key in ("context", "deployer_kubeconfig_env", "secret_materializer_kubeconfig_env", "secrets_encryption_recovery_ref", "network_policy_controller"):
            value = k3s.get(key)
            if not isinstance(value, str) or not value.strip():
                error(f"$.k3s.{key}", "required", "K3s bootstrap metadata is required")
            elif key in {"deployer_kubeconfig_env", "secret_materializer_kubeconfig_env"} and not _ENV_VAR.fullmatch(value):
                error(f"$.k3s.{key}", "identifier", "must be an uppercase environment variable name")
        if k3s.get("deployer_kubeconfig_env") == k3s.get("secret_materializer_kubeconfig_env"):
            error("$.k3s.secret_materializer_kubeconfig_env", "separate_identity", "Secret materializer kubeconfig environment variable must differ from the deployer variable")
        if k3s.get("secrets_encryption_enabled") is not True:
            error("$.k3s.secrets_encryption_enabled", "security", "Secrets at-rest encryption must be explicitly enabled")
        if k3s.get("network_policy_controller") != "kube-router":
            error("$.k3s.network_policy_controller", "security", "K3s must retain the kube-router NetworkPolicy controller")
        _scan_for_secrets(k3s, "$.k3s", error)

    section_keys = {
        "gitlab": {"host", "default_base_branch", "projects", "identity_refs"},
        "discord": {"server_id", "leader_entry_channel_id", "human_user_ids", "bot_identities"},
    }
    for section in ("gitlab", "discord", "profiles"):
        value = root.get(section)
        if value is None:
            error(f"$.{section}", "required", "section is required even when values are pending")
            continue
        if section in section_keys:
            section_value = mapping(value, f"$.{section}")
            if section_value is not None:
                for key in sorted(set(section_value) - section_keys[section]):
                    error(f"$.{section}.{key}", "unknown_field", "field is not part of the environment contract")
        elif not isinstance(value, Mapping):
            error("$.profiles", "type", "expected a profile mapping")
        else:
            for profile, profile_value in value.items():
                profile_path = f"$.profiles.{profile}"
                parsed = mapping(profile_value, profile_path)
                if parsed is not None:
                    for key in sorted(set(parsed) - {"role", "bot_secret_ref", "model", "image"}):
                        error(f"{profile_path}.{key}", "unknown_field", "field is not part of the environment contract")
        _scan_for_secrets(value, f"$.{section}", error)

    for index, decision in enumerate(pending):
        item_path = f"$.pending_decisions[{index}]"
        item = mapping(decision, item_path)
        if item is None:
            continue
        for key in sorted(set(item) - {"key", "owner", "value", "placeholder_policy"}):
            error(f"{item_path}.{key}", "unknown_field", "field is not part of a pending decision")
        for key in ("key", "owner", "placeholder_policy"):
            if not isinstance(item.get(key), str) or not item[key].strip():
                error(f"{item_path}.{key}", "required", "pending decision metadata is required")
        if "value" in item:
            _scan_for_secrets(item["value"], f"{item_path}.value", error)
    return sorted(errors, key=lambda diagnostic: (diagnostic.path, diagnostic.code, diagnostic.message))


def _scan_for_secrets(value: Any, path: str, error: Any) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if isinstance(key, str) and re.search(r"(?:token|password|secret_value|private[_-]?key|api[_-]?key)", key, re.I) and not key.endswith("_ref"):
                error(f"{path}.{key}", "secret_value", "environment contract may contain references only, never credential values")
            _scan_for_secrets(nested, f"{path}.{key}", error)
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _scan_for_secrets(nested, f"{path}[{index}]", error)
    elif isinstance(value, str) and _SECRET_VALUE.search(value):
        error(path, "secret_value", "credential values are not allowed")


def load_environment(path: str | Path, *, require_ready: bool = False) -> list[EnvironmentDiagnostic]:
    document, load_errors = load_environment_document(path)
    if load_errors:
        return load_errors
    return validate_environment(document, require_ready=require_ready)


def load_environment_document(path: str | Path) -> tuple[Any | None, list[EnvironmentDiagnostic]]:
    """Load the non-secret contract for callers that also need path alignment."""

    try:
        with Path(path).open("r", encoding="utf-8") as stream:
            document = load_yaml(stream)
    except (OSError, yaml.YAMLError) as exc:
        return None, [EnvironmentDiagnostic("$", "load", f"unable to load environment: {type(exc).__name__}")]
    return document, []
