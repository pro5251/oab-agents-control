"""Immutable snapshots for non-code review subjects."""

from __future__ import annotations

from hashlib import sha256
import os
from pathlib import Path
import re
import shutil

from .tasks import TASK_ID


class ArtifactError(ValueError):
    pass


_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def snapshot_task_artifact(tasks_root: str | Path, task_id: str, artifact_name: str = "report.md") -> dict[str, str]:
    if not isinstance(task_id, str) or not TASK_ID.fullmatch(task_id):
        raise ArtifactError("unsafe task ID")
    if not _NAME.fullmatch(artifact_name) or "/" in artifact_name or "\\" in artifact_name:
        raise ArtifactError("artifact name must be a safe task output filename")
    task_dir = Path(tasks_root).resolve(strict=True) / task_id
    source = (task_dir / artifact_name).resolve(strict=True)
    if not source.is_file() or not source.is_relative_to(task_dir):
        raise ArtifactError("artifact must be a task-store-managed file")
    digest = sha256(source.read_bytes()).hexdigest()
    artifacts_root = task_dir / "artifacts"
    artifacts_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(artifacts_root, 0o700)
    snapshot = artifacts_root / digest / artifact_name
    snapshot.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(snapshot.parent, 0o700)
    if snapshot.exists() and sha256(snapshot.read_bytes()).hexdigest() != digest:
        raise ArtifactError("artifact snapshot already contains different content")
    if not snapshot.exists():
        shutil.copyfile(source, snapshot)
        os.chmod(snapshot, 0o600)
    return {"kind": "artifact", "artifact_name": artifact_name, "sha256": digest, "snapshot_path": str(snapshot)}
