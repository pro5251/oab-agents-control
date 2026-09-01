"""Local Secret reference resolution with a redaction-safe apply seam."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import stat
from typing import Any, Callable, Mapping

import yaml

from .yaml_utils import load_yaml


_SECRET_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,61}[a-z0-9])?$")
_SECRET_KEY = re.compile(r"^[A-Za-z0-9._-]{1,253}$")


class SecretError(RuntimeError):
    """Raised when a Secret reference cannot be safely materialized."""


@dataclass(frozen=True)
class SecretReference:
    secret_name: str
    key: str

    @classmethod
    def parse(cls, value: str) -> "SecretReference":
        if not isinstance(value, str) or value.count("/") != 1:
            raise SecretError("Secret reference must use secret-name/key syntax")
        secret_name, key = value.split("/", 1)
        if not _SECRET_NAME.fullmatch(secret_name) or not _SECRET_KEY.fullmatch(key):
            raise SecretError("Secret reference contains an unsafe name")
        return cls(secret_name, key)

    def as_text(self) -> str:
        return f"{self.secret_name}/{self.key}"


def load_secret_values(path: str | Path) -> dict[str, str]:
    """Load the ignored local values file without ever including values in errors."""

    values_path = Path(path)
    try:
        metadata = values_path.stat()
    except OSError as exc:
        raise SecretError(f"unable to inspect local Secret values: {type(exc).__name__}") from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
        raise SecretError("local Secret values file must be a regular file with owner-only permissions (0600)")
    try:
        with values_path.open("r", encoding="utf-8") as stream:
            document = load_yaml(stream)
    except (OSError, yaml.YAMLError) as exc:
        raise SecretError(f"unable to load local Secret values: {type(exc).__name__}") from exc
    if not isinstance(document, Mapping) or not set(document).issubset({"secrets", "identity_refs"}) or "secrets" not in document or not isinstance(document["secrets"], Mapping):
        raise SecretError("local Secret values must be a mapping with a secrets field")
    values: dict[str, str] = {}
    for reference, value in document["secrets"].items():
        SecretReference.parse(reference)
        if not isinstance(value, str) or not value:
            raise SecretError(f"local Secret value is empty or not text: {reference}")
        values[reference] = value
    return values


def render_secret_manifests(values: Mapping[str, str], *, namespace: str) -> list[dict[str, Any]]:
    """Build in-memory Secret manifests; callers must pipe them directly to kubectl."""

    if not _SECRET_NAME.fullmatch(namespace):
        raise SecretError("unsafe Secret namespace")
    grouped: dict[str, dict[str, str]] = {}
    for reference, value in values.items():
        parsed = SecretReference.parse(reference)
        if not isinstance(value, str) or not value:
            raise SecretError(f"Secret value is empty: {reference}")
        grouped.setdefault(parsed.secret_name, {})[parsed.key] = value
    return [
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": secret_name, "namespace": namespace},
            "type": "Opaque",
            "stringData": key_values,
        }
        for secret_name, key_values in sorted(grouped.items())
    ]


def materialize_secrets(
    refs: list[str],
    *,
    values_file: str | Path,
    namespace: str,
    apply: Callable[[list[dict[str, Any]]], str],
) -> str:
    """Resolve and apply selected refs; the returned status must be redacted by the adapter."""

    values = load_secret_values(values_file)
    missing = sorted(set(refs) - set(values))
    if missing:
        raise SecretError("unresolved local Secret references: " + ", ".join(missing))
    manifests = render_secret_manifests({reference: values[reference] for reference in refs}, namespace=namespace)
    result = apply(manifests)
    if not isinstance(result, str) or any(value in result for value in values.values()):
        raise SecretError("Secret apply output was not a redacted status")
    return result
