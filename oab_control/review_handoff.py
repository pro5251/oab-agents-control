"""Immutable review-subject handoff through a host-local Git mirror."""

from __future__ import annotations

from pathlib import Path
import re
import subprocess
from typing import Any


class HandoffError(RuntimeError):
    """Raised when an exact review subject cannot be prepared safely."""


_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_TASK = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def prepare_code_review(
    *,
    source_checkout: str | Path,
    reviewer_checkout: str | Path,
    mirror: str | Path,
    task_id: str,
    expected_branch: str,
    base_sha: str,
    base_ref: str | None = None,
) -> dict[str, Any]:
    """Publish and checkout one exact commit without updating a branch ref."""

    source = Path(source_checkout).resolve(strict=True)
    reviewer = Path(reviewer_checkout).resolve(strict=True)
    bare = Path(mirror).resolve(strict=True)
    if not _TASK.fullmatch(task_id):
        raise HandoffError("unsafe task ID")
    if not _SHA.fullmatch(base_sha):
        raise HandoffError("base SHA must be an exact Git object ID")
    if _git(source, "status", "--porcelain"):
        raise HandoffError("developer checkout must be clean")
    if _git(source, "branch", "--show-current") != expected_branch:
        raise HandoffError("developer checkout is on the wrong task branch")
    commit_sha = _git(source, "rev-parse", "--verify", "HEAD^{commit}")
    if not _SHA.fullmatch(commit_sha) or commit_sha == base_sha:
        raise HandoffError("review subject must contain a delivery commit beyond base")
    _run(source, "merge-base", "--is-ancestor", base_sha, commit_sha)
    if _git(bare, "rev-parse", "--is-bare-repository") != "true":
        raise HandoffError("review mirror must be a local bare repository")

    review_ref = f"refs/oab/reviews/{task_id}/{commit_sha}"
    current = _optional_git(bare, "rev-parse", "--verify", review_ref)
    if current and current != commit_sha:
        raise HandoffError("review ref already points to a different commit")
    if not current:
        _run(source, "push", str(bare), f"{commit_sha}:{review_ref}")
    if _git(bare, "rev-parse", "--verify", review_ref) != commit_sha:
        raise HandoffError("published review ref does not match the subject")

    _run(reviewer, "fetch", str(bare), f"{review_ref}:{review_ref}")
    _run(reviewer, "checkout", "--detach", commit_sha)
    if _git(reviewer, "rev-parse", "HEAD") != commit_sha:
        raise HandoffError("reviewer checkout does not match the subject")
    return {
        "kind": "git_commit",
        "repository": str(source),
        "base_ref": base_ref or expected_branch,
        "base_sha": base_sha,
        "commit_sha": commit_sha,
        "review_ref": review_ref,
    }


def resolve_commit(checkout: str | Path, ref: str) -> str:
    path = Path(checkout).resolve(strict=True)
    value = _git(path, "rev-parse", "--verify", f"{ref}^{{commit}}")
    if not _SHA.fullmatch(value):
        raise HandoffError("base ref did not resolve to an exact Git SHA")
    return value


def _git(cwd: Path, *args: str) -> str:
    result = _run(cwd, *args)
    return result.stdout.strip()


def _optional_git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return result.stdout.strip() if result.returncode == 0 else ""


def _run(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(["git", *args], cwd=cwd, check=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as exc:
        raise HandoffError(f"Git review handoff failed during {args[0]}") from exc
