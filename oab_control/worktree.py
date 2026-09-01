"""Independent repository checkout materialization for agent worktrees.

The manager never calls ``git worktree``.  Each grant is a full clone with its
own ``.git`` directory, so a Pod does not need the source collection or a
shared Git metadata directory.  GitLab remotes are supplied by the operator's
environment contract; a local source path is used only as a seed.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any, Mapping


class WorktreeError(RuntimeError):
    """Raised when a checkout cannot be materialized without risking state."""


BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
AGENT_ID = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
MARKER_NAME = ".oab-agent-worktree.json"


@dataclass(frozen=True)
class Checkout:
    agent_id: str
    repository: str
    path: str
    container_mount_path: str
    access: str
    branch: str
    base_branch: str
    origin: str
    created: bool

    @property
    def read_only(self) -> bool:
        return self.access == "read"


def _safe_branch(value: str) -> str:
    if not isinstance(value, str) or not BRANCH.fullmatch(value) or value.startswith("/") or value.endswith("/") or ".." in value.split("/"):
        raise WorktreeError("unsafe branch name")
    return value


def _redact(value: str) -> str:
    value = re.sub(r"((?:https?|ssh)://)([^/@]+):([^/@]+)@", r"\1<redacted>@", value)
    value = re.sub(r"(?:glpat-|gh[pousr]_|sk-|xox[baprs]-)[A-Za-z0-9_-]+", "<redacted>", value)
    return value


def _git(args: list[str], *, cwd: Path | None = None) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
            env={"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        raise WorktreeError(f"git command failed: {_redact(detail).strip() or type(exc).__name__}") from exc
    return result.stdout.strip()


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return child != parent
    except ValueError:
        return False


def _assert_git_repository(path: Path) -> None:
    if not path.is_dir():
        raise WorktreeError(f"repository path does not exist: {path}")
    if _git(["-C", str(path), "rev-parse", "--is-inside-work-tree"]) != "true":
        raise WorktreeError(f"path is not a Git work tree: {path}")


def _origin(path: Path) -> str:
    try:
        return _git(["-C", str(path), "remote", "get-url", "origin"])
    except WorktreeError as exc:
        raise WorktreeError(f"repository has no origin remote: {path}") from exc


def _safe_remote(value: str, *, allow_local: bool = False) -> str:
    """Accept GitLab-style delivery URLs, never a local/file remote or URL credential."""

    if not isinstance(value, str) or not value.strip() or any(character in value for character in ("\0", "\r", "\n", " ", "\t")):
        raise WorktreeError("delivery remote must be a non-empty URL without control characters")
    remote = value.strip()
    if (remote.startswith(("/", "./", "../")) or (remote.startswith("file:") and not allow_local)) or re.search(r"(?:https?|ssh)://[^/\s:@]+:[^@\s]+@", remote, re.I):
        raise WorktreeError("delivery remote must not be a local path or contain credentials")
    if allow_local and remote.startswith("file:"):
        return remote
    if not (remote.startswith(("https://", "http://", "ssh://")) or re.fullmatch(r"[^/@:\s]+@[^/:\s]+:.+", remote)):
        raise WorktreeError("delivery remote must be an explicit HTTP(S)/SSH GitLab remote")
    return remote


class WorktreeManager:
    """Materialize and inspect one independent checkout per repository grant."""

    def __init__(self, *, remotes: Mapping[str, str], allow_local_remotes: bool = False):
        if not isinstance(allow_local_remotes, bool):
            raise WorktreeError("allow_local_remotes must be a boolean")
        self.remotes = {str(Path(repo).resolve(strict=False)): remote for repo, remote in remotes.items()}
        self.allow_local_remotes = allow_local_remotes

    def materialize(
        self,
        *,
        agent_id: str,
        agent: Mapping[str, Any],
        task_id: str,
        branch: str | None = None,
    ) -> list[Checkout]:
        """Create or reuse all grants for one agent and one active task.

        A pre-existing checkout is never silently reset or deleted.  If it has
        local changes, a branch switch fails closed so a human can reconcile it.
        """

        branch = _safe_branch(branch or f"task/{task_id}")
        worktree = agent["worktree"]
        worktree_root = Path(worktree["path"]).resolve(strict=False)
        worktree_root.mkdir(parents=True, exist_ok=True)
        self._check_marker(worktree_root, agent_id)
        result: list[Checkout] = []
        created_paths: list[Path] = []
        try:
            for grant in agent["repository_grants"]:
                source = Path(grant["repository"]).resolve(strict=False)
                _assert_git_repository(source)
                configured_remote = self.remotes.get(str(source))
                if not configured_remote:
                    raise WorktreeError(f"missing configured GitLab remote for repository: {source}")
                remote = _safe_remote(configured_remote, allow_local=self.allow_local_remotes)
                checkout_subpath = Path(*grant["checkout_subpath"].split("/"))
                checkout_path = (worktree_root / checkout_subpath).resolve(strict=False)
                if not _inside(checkout_path, worktree_root):
                    raise WorktreeError(f"checkout escapes agent worktree: {checkout_path}")
                checkout_path.parent.mkdir(parents=True, exist_ok=True)
                created = not checkout_path.exists()
                if created:
                    created_paths.append(checkout_path)
                    # --no-hardlinks makes the clone independent even when the
                    # source and target are on the same local filesystem.
                    _git(["clone", "--no-hardlinks", str(source), str(checkout_path)])
                    _git(["-C", str(checkout_path), "remote", "set-url", "origin", remote])
                else:
                    _assert_git_repository(checkout_path)
                    if _origin(checkout_path) != remote:
                        raise WorktreeError(f"checkout origin differs from configured delivery remote: {checkout_path}")

                base_branch = _safe_branch(grant["base_branch"])
                if not base_branch.startswith("origin/"):
                    raise WorktreeError(f"base branch must be an origin remote ref: {base_branch}")
                _git(["-C", str(checkout_path), "fetch", "--prune", "origin", base_branch.removeprefix("origin/")])
                dirty = _git(["-C", str(checkout_path), "status", "--porcelain"])
                current = _git(["-C", str(checkout_path), "branch", "--show-current"])
                if dirty and current != branch:
                    raise WorktreeError(f"checkout has uncommitted changes and cannot switch branches: {checkout_path}")
                if current != branch:
                    existing = _git(["-C", str(checkout_path), "branch", "--list", branch])
                    if existing:
                        _git(["-C", str(checkout_path), "switch", branch])
                    else:
                        _git(["-C", str(checkout_path), "switch", "--create", branch, f"origin/{base_branch.removeprefix('origin/')}"])
                result.append(
                    Checkout(
                        agent_id=agent_id,
                        repository=str(source),
                        path=str(checkout_path),
                        container_mount_path=f"{worktree['container_mount_path']}/{grant['checkout_subpath']}",
                        access=grant["access"],
                        branch=branch,
                        base_branch=base_branch,
                        origin=remote,
                        created=created,
                    )
                )
            self._write_marker(worktree_root, agent_id)
            return result
        except Exception:
            # Only remove checkouts created by this invocation.  An existing
            # dirty worktree remains untouched for explicit reconciliation.
            for path in reversed(created_paths):
                try:
                    if path.is_dir() and not path.is_symlink():
                        shutil.rmtree(path)
                    else:
                        path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise

    def retire_agent(self, *, agent_id: str, worktree_path: str, confirmed: bool) -> None:
        """Remove an agent worktree only after explicit retirement approval."""

        if not confirmed:
            raise WorktreeError("agent retirement requires explicit confirmation")
        target = Path(worktree_path).resolve(strict=False)
        if not target.is_dir():
            raise WorktreeError("agent worktree does not exist; reconcile before retirement")
        if target == Path(target.anchor):
            raise WorktreeError("refusing to remove a filesystem root")
        self._check_marker(target, agent_id, required=True)
        shutil.rmtree(target)

    def cleanup_task(self, *, checkouts: list[Checkout], confirmed: bool) -> None:
        """Close a task branch while retaining the agent's worktree.

        Tracked edits are never discarded implicitly.  The operator must
        first deliver or reconcile them; after confirmation only untracked
        files are cleaned, the checkout is detached at its configured base,
        and the task branch is deleted.
        """

        if not confirmed:
            raise WorktreeError("task cleanup requires explicit confirmation")
        if not checkouts:
            raise WorktreeError("task cleanup requires at least one checkout")
        for checkout in checkouts:
            path = Path(checkout.path).resolve(strict=False)
            _assert_git_repository(path)
            current = _git(["-C", str(path), "branch", "--show-current"])
            if current != checkout.branch:
                raise WorktreeError(f"checkout branch does not match task envelope: {path}")
            dirty_lines = _git(["-C", str(path), "status", "--porcelain"]).splitlines()
            tracked = [line for line in dirty_lines if not line.startswith("??")]
            if tracked:
                raise WorktreeError(f"tracked changes require reconciliation before cleanup: {path}")
        for checkout in checkouts:
            path = Path(checkout.path).resolve(strict=False)
            base = _safe_branch(checkout.base_branch)
            if not base.startswith("origin/"):
                raise WorktreeError(f"base branch must be an origin remote ref: {base}")
            remote_branch = base.removeprefix("origin/")
            _git(["-C", str(path), "fetch", "--prune", "origin", remote_branch])
            _git(["-C", str(path), "clean", "-fd"])
            _git(["-C", str(path), "switch", "--detach", base])
            _git(["-C", str(path), "branch", "-D", checkout.branch])

    @staticmethod
    def _check_marker(target: Path, agent_id: str, *, required: bool = False) -> None:
        if not AGENT_ID.fullmatch(agent_id):
            raise WorktreeError("unsafe agent ID")
        marker = target / MARKER_NAME
        if not marker.exists():
            if required:
                raise WorktreeError("refusing to retire an unregistered worktree (marker is missing)")
            return
        try:
            document = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorktreeError("worktree marker is unreadable") from exc
        if not isinstance(document, dict) or document.get("version") != 1 or document.get("agent_id") != agent_id:
            raise WorktreeError("worktree marker does not belong to the requested agent")

    @staticmethod
    def _write_marker(target: Path, agent_id: str) -> None:
        marker = target / MARKER_NAME
        if marker.exists():
            return
        temporary = marker.with_name(f".{marker.name}.tmp")
        try:
            temporary.write_text(
                json.dumps({"version": 1, "agent_id": agent_id}, ensure_ascii=False, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, marker)
        except OSError as exc:
            try:
                temporary.unlink()
            except OSError:
                pass
            raise WorktreeError(f"unable to register worktree: {type(exc).__name__}") from exc
