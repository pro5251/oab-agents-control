from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest

from oab_control.evidence import EvidenceError, collect, locate_checkout, merge_into
from oab_control.tasks import Task


def git(*args: str, cwd: Path | None = None) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def fixture(root: Path, *, commit_work: bool = True, dirty: bool = False) -> tuple[Task, dict]:
    """A checkout on a task branch, seeded so origin/develop resolves."""

    worktree = root / "worktrees" / "developer"
    checkout = worktree / "col" / "repo"
    checkout.mkdir(parents=True)
    git("init", "-q", str(checkout))
    git("config", "user.email", "t@example.invalid", cwd=checkout)
    git("config", "user.name", "Test", cwd=checkout)
    (checkout / "base.txt").write_text("base\n", encoding="utf-8")
    git("add", "-A", cwd=checkout)
    git("commit", "-qm", "base", cwd=checkout)
    git("branch", "-M", "develop", cwd=checkout)
    git("update-ref", "refs/remotes/origin/develop", "HEAD", cwd=checkout)
    git("switch", "-qc", "task/t1", cwd=checkout)
    if commit_work:
        (checkout / "work.txt").write_text("work\n", encoding="utf-8")
        git("add", "-A", cwd=checkout)
        git("commit", "-qm", "work", cwd=checkout)
    if dirty:
        (checkout / "scratch.txt").write_text("uncommitted\n", encoding="utf-8")

    catalog = {
        "agents": {
            "developer": {
                "worktree": {"path": str(worktree)},
                "repository_grants": [
                    {
                        "repository": "/src/repo",
                        "checkout_subpath": "col/repo",
                        "access": "write",
                        "base_branch": "origin/develop",
                    }
                ],
            }
        }
    }
    task = Task(
        task_id="t1", kind="code", goal="g", scope="s", canonical_sources=(),
        agent_id="developer", repository="/src/repo", checkout_subpath="col/repo",
        worktree_path=str(worktree), container_mount_path="/workspaces/developer/col/repo",
        branch="task/t1", base_branch="origin/develop", delivery_owner="developer",
        gitlab_identity_ref="ref", tests=(), completion_marker="m", checkpoint="c",
        deadline="2099-01-01T00:00:00Z", reply_to="1",
    )
    return task, catalog


class EvidenceCollectionTests(unittest.TestCase):
    def test_collects_the_fields_the_gate_can_check(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            task, catalog = fixture(Path(directory))
            evidence = collect(task, catalog)
        self.assertEqual(set(evidence.fields), {"commit_sha", "delivery_branch", "delivery_repository", "delivery_owner"})
        self.assertEqual(evidence.fields["delivery_branch"], "task/t1")
        self.assertEqual(evidence.fields["delivery_owner"], "developer")
        self.assertRegex(evidence.fields["commit_sha"], r"^[0-9a-f]{40}$")
        self.assertEqual(evidence.context["commits_ahead"], 1)
        self.assertEqual(evidence.context["changed_file_count"], 1)

    def test_push_ref_and_merge_request_are_never_collected(self) -> None:
        """Agents have no push path, so those remain the operator's claims."""

        with tempfile.TemporaryDirectory() as directory:
            task, catalog = fixture(Path(directory))
            evidence = collect(task, catalog)
        self.assertNotIn("push_ref", evidence.fields)
        self.assertNotIn("merge_request", evidence.fields)

    def test_refuses_when_nothing_was_committed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            task, catalog = fixture(Path(directory), commit_work=False)
            with self.assertRaises(EvidenceError) as caught:
                collect(task, catalog)
        self.assertIn("nothing to accept", str(caught.exception))

    def test_refuses_a_dirty_checkout(self) -> None:
        """Uncommitted changes make the delivered content indeterminate."""

        with tempfile.TemporaryDirectory() as directory:
            task, catalog = fixture(Path(directory), dirty=True)
            with self.assertRaises(EvidenceError) as caught:
                collect(task, catalog)
        self.assertIn("uncommitted", str(caught.exception))

    def test_refuses_when_the_checkout_is_on_another_branch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task, catalog = fixture(root)
            git("switch", "-q", "develop", cwd=root / "worktrees" / "developer" / "col" / "repo")
            with self.assertRaises(EvidenceError) as caught:
                collect(task, catalog)
        self.assertIn("task declares", str(caught.exception))

    def test_refuses_when_the_checkout_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            task, catalog = fixture(Path(directory))
            import shutil

            shutil.rmtree(Path(directory) / "worktrees" / "developer" / "col" / "repo")
            with self.assertRaises(EvidenceError) as caught:
                collect(task, catalog)
        self.assertIn("not materialized", str(caught.exception))

    def test_path_comes_from_the_catalog_not_the_task(self) -> None:
        """The task record selects a grant; it is not an authorization source."""

        with tempfile.TemporaryDirectory() as directory:
            task, catalog = fixture(Path(directory))
            catalog["agents"]["developer"]["repository_grants"] = []
            with self.assertRaises(EvidenceError) as caught:
                locate_checkout(task, catalog)
        self.assertIn("exactly one catalog grant", str(caught.exception))


class EvidenceMergeTests(unittest.TestCase):
    def test_fills_verified_fields_alongside_attested_ones(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            task, catalog = fixture(Path(directory))
            evidence = collect(task, catalog)
            merged = merge_into({"developer_tests": "passed"}, evidence)
        self.assertEqual(merged["developer_tests"], "passed")
        self.assertEqual(merged["delivery_branch"], "task/t1")

    def test_a_disagreement_stops_rather_than_being_overwritten(self) -> None:
        """Either the transcription is wrong or the report it came from was."""

        with tempfile.TemporaryDirectory() as directory:
            task, catalog = fixture(Path(directory))
            evidence = collect(task, catalog)
            with self.assertRaises(EvidenceError) as caught:
                merge_into({"commit_sha": "deadbeef"}, evidence)
        self.assertIn("does not match the checkout", str(caught.exception))

    def test_matching_supplied_value_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            task, catalog = fixture(Path(directory))
            evidence = collect(task, catalog)
            merged = merge_into({"commit_sha": evidence.fields["commit_sha"]}, evidence)
        self.assertEqual(merged["commit_sha"], evidence.fields["commit_sha"])


if __name__ == "__main__":
    unittest.main()
