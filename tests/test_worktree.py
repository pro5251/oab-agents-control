from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest

from oab_control.worktree import WorktreeError, WorktreeManager


def git(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def repository_fixture(root: Path) -> tuple[Path, Path]:
    remote = root / "remote.git"
    source = root / "repositories" / "team-a" / "service-x"
    git("init", "--bare", str(remote))
    source.mkdir(parents=True)
    git("init", str(source))
    git("config", "user.email", "test@example.invalid", cwd=source)
    git("config", "user.name", "Test", cwd=source)
    (source / "README.md").write_text("seed\n", encoding="utf-8")
    git("add", "README.md", cwd=source)
    git("commit", "-m", "seed", cwd=source)
    git("branch", "-M", "develop", cwd=source)
    git("remote", "add", "origin", str(remote), cwd=source)
    git("push", "--set-upstream", "origin", "develop", cwd=source)
    return source, remote


def agent(root: Path, source: Path) -> dict:
    return {
        "worktree": {"path": str(root / "worktrees" / "developer"), "container_mount_path": "/workspace/developer"},
        "repository_grants": [{
            "repository": str(source),
            "checkout_subpath": "team-a/service-x",
            "access": "write",
            "base_branch": "origin/develop",
        }],
    }


class WorktreeTests(unittest.TestCase):
    def test_materialize_uses_independent_clone_and_configured_origin(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, remote = repository_fixture(root)
            manager = WorktreeManager(remotes={str(source): remote.as_uri()}, allow_local_remotes=True)
            checkouts = manager.materialize(agent_id="developer", agent=agent(root, source), task_id="task-001")
            checkout = checkouts[0]
            target = Path(checkout.path)
            self.assertTrue(checkout.created)
            self.assertTrue((target.parent.parent / ".oab-agent-worktree.json").is_file())
            self.assertTrue((target / ".git").is_dir())
            self.assertEqual(git("remote", "get-url", "origin", cwd=target), remote.as_uri())
            self.assertEqual(git("branch", "--show-current", cwd=target), "task/task-001")
            self.assertEqual((source / "README.md").read_text(encoding="utf-8"), "seed\n")

            (target / "README.md").write_text("agent change\n", encoding="utf-8")
            second = manager.materialize(agent_id="developer", agent=agent(root, source), task_id="task-001")[0]
            self.assertFalse(second.created)
            self.assertEqual((target / "README.md").read_text(encoding="utf-8"), "agent change\n")

    def test_two_agents_get_separate_checkouts_of_one_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, remote = repository_fixture(root)
            manager = WorktreeManager(remotes={str(source): remote.as_uri()}, allow_local_remotes=True)
            first = manager.materialize(agent_id="developer", agent=agent(root, source), task_id="task-a")[0]
            reviewer = agent(root, source)
            reviewer["worktree"]["path"] = str(root / "worktrees" / "reviewer")
            second = manager.materialize(agent_id="reviewer", agent=reviewer, task_id="task-b")[0]
            self.assertNotEqual(first.path, second.path)
            self.assertNotEqual(Path(first.path) / ".git", source / ".git")
            self.assertNotEqual(Path(second.path) / ".git", source / ".git")

    def test_missing_remote_and_unsafe_task_branch_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, remote = repository_fixture(root)
            agent_definition = agent(root, source)
            with self.assertRaises(WorktreeError):
                WorktreeManager(remotes={}).materialize(agent_id="developer", agent=agent_definition, task_id="safe")
            manager = WorktreeManager(remotes={str(source): remote.as_uri()}, allow_local_remotes=True)
            with self.assertRaises(WorktreeError):
                manager.materialize(agent_id="developer", agent=agent_definition, task_id="../escape")

    def test_production_remote_map_rejects_local_file_remote(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, remote = repository_fixture(root)
            with self.assertRaises(WorktreeError):
                WorktreeManager(remotes={str(source): remote.as_uri()}).materialize(
                    agent_id="developer", agent=agent(root, source), task_id="strict"
                )

    def test_retirement_requires_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "worktrees" / "developer"
            root.mkdir(parents=True)
            (root / "state").write_text("state", encoding="utf-8")
            manager = WorktreeManager(remotes={})
            with self.assertRaises(WorktreeError):
                manager.retire_agent(agent_id="developer", worktree_path=str(root), confirmed=False)
            (root / ".oab-agent-worktree.json").write_text('{"agent_id":"developer","version":1}\n', encoding="utf-8")
            manager.retire_agent(agent_id="developer", worktree_path=str(root), confirmed=True)
            self.assertFalse(root.exists())

    def test_cleanup_removes_only_untracked_and_task_branch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, remote = repository_fixture(root)
            manager = WorktreeManager(remotes={str(source): remote.as_uri()}, allow_local_remotes=True)
            checkout = manager.materialize(agent_id="developer", agent=agent(root, source), task_id="cleanup")[0]
            path = Path(checkout.path)
            (path / "scratch.txt").write_text("temporary\n", encoding="utf-8")
            manager.cleanup_task(checkouts=[checkout], confirmed=True)
            self.assertFalse((path / "scratch.txt").exists())
            self.assertEqual(git("branch", "--show-current", cwd=path), "")
            self.assertNotIn("task/cleanup", git("branch", cwd=path))
            self.assertTrue((path.parent.parent / ".oab-agent-worktree.json").is_file())

    def test_cleanup_refuses_tracked_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, remote = repository_fixture(root)
            manager = WorktreeManager(remotes={str(source): remote.as_uri()}, allow_local_remotes=True)
            checkout = manager.materialize(agent_id="developer", agent=agent(root, source), task_id="dirty")[0]
            path = Path(checkout.path)
            (path / "README.md").write_text("must reconcile\n", encoding="utf-8")
            with self.assertRaises(WorktreeError):
                manager.cleanup_task(checkouts=[checkout], confirmed=True)
            self.assertEqual(git("branch", "--show-current", cwd=path), "task/dirty")


if __name__ == "__main__":
    unittest.main()
