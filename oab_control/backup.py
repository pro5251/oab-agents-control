"""Local backup and restore primitives for the control host.

The implementation intentionally copies only declared control-state roots.  It
does not claim that a filesystem is encrypted by itself: callers must provide
an operator attestation that the destination is an encrypted external disk or
NAS.  Secret values files are excluded by filename and are never restored.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any, Mapping
import uuid


class BackupError(RuntimeError):
    """Raised when a backup/restore operation cannot remain recoverable."""


COMPONENTS = ("catalog", "coordination_repository", "agent_worktrees_root", "k3s_state")
_EXCLUDED_NAMES = {"secrets.yaml", "secrets.yml", "secrets.json", ".env", ".env.local"}
_CREDENTIAL = re.compile(
    r"(?:"
    r"glpat-[A-Za-z0-9_-]{16,}|"
    r"gh[pousr]_[A-Za-z0-9_]{16,}|"
    r"sk-[A-Za-z0-9_-]{16,}|"
    r"xox[baprs]-[A-Za-z0-9_-]{16,}|"
    r"(?:https?|ssh)://[^/\s:@]+:[^@\s]+@|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r")",
    re.I,
)


@dataclass(frozen=True)
class BackupResult:
    path: str
    manifest: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {"path": self.path, "manifest": self.manifest}


class LocalBackup:
    """Create and verify a deterministic, secret-excluding local backup."""

    def __init__(self, target_root: str | Path):
        self.target_root = Path(target_root).resolve(strict=False)

    def create(
        self,
        sources: Mapping[str, str | Path],
        *,
        encryption_attestation: str,
        backup_id: str | None = None,
    ) -> BackupResult:
        self._validate_sources(sources)
        attestation = _safe_attestation(encryption_attestation)
        self.target_root.mkdir(parents=True, exist_ok=True)
        name = backup_id or f"backup-{_timestamp()}-{uuid.uuid4().hex[:8]}"
        if not re.fullmatch(r"backup-[0-9TZ-]+-[0-9a-f]{8}", name):
            raise BackupError("unsafe backup ID")
        target = self.target_root / name
        if target.exists():
            raise BackupError("backup target already exists")
        target.mkdir()
        entries: list[dict[str, Any]] = []
        component_metadata: dict[str, dict[str, str]] = {}
        excluded: list[str] = []
        try:
            for component in COMPONENTS:
                source = Path(sources[component]).resolve(strict=True)
                destination = target / component
                if source.is_file():
                    destination.mkdir(parents=True)
                    output = destination / source.name
                    self._copy_file(source, output, f"{component}/{source.name}", entries, excluded)
                    kind = "file"
                elif source.is_dir():
                    destination.mkdir(parents=True)
                    self._copy_tree(source, destination, component, entries, excluded)
                    kind = "directory"
                else:
                    raise BackupError(f"backup source is not a regular file/directory: {component}")
                component_metadata[component] = {"source_kind": kind, "source_basename": source.name}
            manifest = {
                "version": 1,
                "created_at": _now(),
                "encrypted_target_attested": True,
                "attestation": attestation,
                "secret_values_included": False,
                "components": list(COMPONENTS),
                "component_metadata": component_metadata,
                "excluded_paths": sorted(excluded),
                "files": sorted(entries, key=lambda item: item.get("path", "")),
            }
            _atomic_text(target / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        except Exception:
            shutil.rmtree(target, ignore_errors=True)
            raise
        return BackupResult(str(target), manifest)

    def verify(self, backup_path: str | Path) -> dict[str, Any]:
        raw_backup = Path(backup_path)
        if raw_backup.is_symlink():
            raise BackupError("backup path must not be a symlink")
        backup = raw_backup.resolve(strict=True)
        if not backup.is_dir():
            raise BackupError("backup path must be a regular directory")
        manifest_path = backup / "manifest.json"
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise BackupError("backup manifest is not a regular file")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BackupError("backup manifest is unreadable") from exc
        if not isinstance(manifest, Mapping) or manifest.get("version") != 1 or manifest.get("secret_values_included") is not False:
            raise BackupError("backup manifest is not a supported secret-free version")
        if manifest.get("components") != list(COMPONENTS) or manifest.get("encrypted_target_attested") is not True:
            raise BackupError("backup manifest is missing required component/attestation metadata")
        component_metadata = manifest.get("component_metadata")
        if not isinstance(component_metadata, Mapping) or set(component_metadata) != set(COMPONENTS):
            raise BackupError("backup manifest component metadata is incomplete")
        for component in COMPONENTS:
            metadata = component_metadata.get(component)
            if not isinstance(metadata, Mapping) or metadata.get("source_kind") not in {"file", "directory"} or not isinstance(metadata.get("source_basename"), str) or Path(metadata["source_basename"]).name != metadata["source_basename"]:
                raise BackupError(f"backup manifest has invalid metadata for component: {component}")
        files = manifest.get("files")
        if not isinstance(files, list):
            raise BackupError("backup manifest files must be a list")
        seen_paths: set[str] = set()
        for item in files:
            if not isinstance(item, Mapping) or not isinstance(item.get("path"), str) or not isinstance(item.get("sha256"), str) or item.get("component") not in COMPONENTS or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"]):
                raise BackupError("backup manifest contains an invalid file entry")
            relative = Path(item["path"])
            if relative.is_absolute() or ".." in relative.parts or not item["path"].startswith(f"{item['component']}/"):
                raise BackupError("backup manifest contains an unsafe relative path")
            if item["path"] in seen_paths:
                raise BackupError("backup manifest contains a duplicate file entry")
            seen_paths.add(item["path"])
            path = (backup / item["path"]).resolve(strict=False)
            raw_path = backup / item["path"]
            if raw_path.is_symlink() or not path.is_file() or not path.is_relative_to(backup):
                raise BackupError(f"backup file is missing or escapes backup root: {item['path']}")
            if _sha256(path) != item["sha256"]:
                raise BackupError(f"backup checksum mismatch: {item['path']}")
        # A copied-in secret file or credential-shaped payload invalidates the
        # backup even if an attacker edits the manifest to omit it.
        for path in backup.rglob("*"):
            if path.is_symlink():
                raise BackupError("backup contains a symlink")
            if not path.is_file() or path == manifest_path:
                continue
            if _excluded(path.name):
                raise BackupError("backup contains an excluded Secret-values filename")
            try:
                sample = path.read_bytes()
            except OSError as exc:
                raise BackupError("backup file is unreadable") from exc
            if _CREDENTIAL.search(sample.decode("utf-8", errors="ignore")):
                raise BackupError("backup contains a credential-shaped value")
        return dict(manifest)

    def restore(
        self,
        backup_path: str | Path,
        destinations: Mapping[str, str | Path],
        *,
        confirmed: bool,
    ) -> dict[str, Any]:
        if not confirmed:
            raise BackupError("restore requires explicit confirmation")
        manifest = self.verify(backup_path)
        backup = Path(backup_path).resolve(strict=True)
        self._validate_destinations(destinations)
        target_paths = {component: Path(destinations[component]).resolve(strict=False) for component in COMPONENTS}
        if any(final == backup or final.is_relative_to(backup) for final in target_paths.values()):
            raise BackupError("restore destination must not overwrite the backup")
        component_metadata = manifest["component_metadata"]
        staging_root = Path(tempfile.mkdtemp(prefix=".oab-restore-", dir=str(self.target_root.parent)))
        staged: dict[str, Path] = {}
        installed: list[Path] = []
        try:
            for component in COMPONENTS:
                source = backup / component
                destination = staging_root / component
                if not source.exists():
                    raise BackupError(f"backup component is missing: {component}")
                if component_metadata[component].get("source_kind") == "file":
                    source_files = [item for item in manifest["files"] if item.get("path", "").startswith(f"{component}/")]
                    if not source_files:
                        raise BackupError(f"backup file component has no content: {component}")
                    destination.mkdir(parents=True)
                    for item in source_files:
                        relative = Path(item["path"]).relative_to(component)
                        output = destination / relative
                        output.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(backup / item["path"], output)
                    staged[component] = destination / Path(next(item for item in source_files)["path"]).name
                else:
                    shutil.copytree(source, destination, symlinks=False)
                    staged[component] = destination
            for component, final in target_paths.items():
                if final.exists():
                    raise BackupError(f"restore destination must be absent/clean: {final}")
                final.parent.mkdir(parents=True, exist_ok=True)
            restored: list[str] = []
            for component, final in target_paths.items():
                os.replace(staged[component], final)
                installed.append(final)
                restored.append(component)
        except Exception:
            for path in reversed(installed):
                try:
                    if path.is_dir() and not path.is_symlink():
                        shutil.rmtree(path)
                    else:
                        path.unlink(missing_ok=True)
                except OSError:
                    pass
            shutil.rmtree(staging_root, ignore_errors=True)
            raise
        shutil.rmtree(staging_root, ignore_errors=True)
        return {"backup": str(backup), "restored_components": restored, "secret_values_restored": False}

    def _validate_sources(self, sources: Mapping[str, str | Path]) -> None:
        if set(sources) != set(COMPONENTS):
            raise BackupError("backup sources must contain exactly the declared control-state components")
        parsed: list[Path] = []
        for component in COMPONENTS:
            candidate = Path(sources[component]).resolve(strict=False)
            if not candidate.is_absolute() or any(part in {".", ".."} for part in Path(str(sources[component])).parts):
                raise BackupError(f"backup source must be an explicit absolute path: {component}")
            if not candidate.exists():
                raise BackupError(f"backup source does not exist: {component}")
            if candidate == self.target_root or self.target_root.is_relative_to(candidate):
                raise BackupError("backup target must not be inside a backup source")
            parsed.append(candidate)
        for index, left in enumerate(parsed):
            for right in parsed[index + 1 :]:
                if left == right or left.is_relative_to(right) or right.is_relative_to(left):
                    raise BackupError("backup sources must not overlap")

    def _validate_destinations(self, destinations: Mapping[str, str | Path]) -> None:
        if set(destinations) != set(COMPONENTS):
            raise BackupError("restore destinations must contain exactly the declared control-state components")
        parsed: list[Path] = []
        for component in COMPONENTS:
            raw = str(destinations[component])
            candidate = Path(raw).resolve(strict=False)
            if not candidate.is_absolute() or any(part in {".", ".."} for part in Path(raw).parts):
                raise BackupError(f"restore destination must be an explicit absolute path: {component}")
            parsed.append(candidate)
        for index, left in enumerate(parsed):
            for right in parsed[index + 1 :]:
                if left == right or left.is_relative_to(right) or right.is_relative_to(left):
                    raise BackupError("restore destinations must not overlap")

    def _copy_tree(self, source: Path, destination: Path, component: str, entries: list[dict[str, Any]], excluded: list[str]) -> None:
        for current, directories, files in os.walk(source, followlinks=False):
            current_path = Path(current)
            directories[:] = sorted(directory for directory in directories if not (current_path / directory).is_symlink())
            for name in sorted(files):
                source_file = current_path / name
                relative = source_file.relative_to(source)
                output = destination / relative
                relative_text = f"{component}/{relative.as_posix()}"
                self._copy_file(source_file, output, relative_text, entries, excluded)

    @staticmethod
    def _copy_file(source: Path, output: Path, relative_text: str, entries: list[dict[str, Any]], excluded: list[str]) -> None:
        if source.is_symlink():
            raise BackupError(f"backup refuses symlinked file: {relative_text}")
        if not source.is_file():
            raise BackupError(f"backup refuses non-regular file: {relative_text}")
        if _excluded(source.name):
            excluded.append(relative_text)
            return
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, output)
        entries.append({"component": relative_text.split("/", 1)[0], "path": relative_text, "sha256": _sha256(output), "bytes": output.stat().st_size})


def _excluded(name: str) -> bool:
    return name.lower() in _EXCLUDED_NAMES or name.lower().endswith((".secret", ".secret.yaml", ".secret.yml"))


def _safe_attestation(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 200 or _CREDENTIAL.search(value) or re.search(r"(?:token|password|secret)\s*[:=]", value, re.I):
        raise BackupError("encryption attestation must be a short non-secret statement")
    return value.strip()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _atomic_text(path: Path, content: str) -> None:
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
        raise BackupError(f"unable to persist backup manifest: {type(exc).__name__}") from exc
