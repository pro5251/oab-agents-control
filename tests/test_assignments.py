from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from oab_control.assignments import AssignmentError, AssignmentStore
from oab_control.catalog import validate_catalog
from oab_control.tasks import Task, TaskStore
from test_catalog import catalog
from test_tasks import task


class AssignmentStoreTests(unittest.TestCase):
    def fixture(self, root: Path) -> tuple[dict, TaskStore, Path]:
        normalized, diagnostics = validate_catalog(catalog(root))
        self.assertEqual(diagnostics, [])
        assert normalized is not None
        developer = normalized["agents"]["developer"]
        grant = developer["repository_grants"][0]
        checkout = Path(developer["worktree"]["path"]) / grant["checkout_subpath"]
        checkout.mkdir(parents=True, exist_ok=True)
        delivery = Task(**(task().as_dict() | {
            "repository": grant["repository"],
            "checkout_subpath": grant["checkout_subpath"],
            "worktree_path": developer["worktree"]["path"],
            "container_mount_path": f'{developer["worktree"]["container_mount_path"]}/{grant["checkout_subpath"]}',
            "base_branch": grant["base_branch"],
            "gitlab_identity_ref": developer["delivery"]["gitlab_identity_ref"],
            "reply_to": developer["discord"]["work_channel_id"],
        }))
        tasks = TaskStore(root / "tasks")
        tasks.create(delivery, actor="leader")
        tasks.transition(delivery.task_id, "assigned", actor="leader")
        inbox_root = root / "assignment-inboxes"
        (inbox_root / "developer").mkdir(parents=True)
        return normalized, tasks, inbox_root

    def test_prepare_execution_persists_immutable_catalog_binding_and_repo_external_brief(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            normalized, tasks, inbox_root = self.fixture(root)
            assignment = AssignmentStore(tasks.root).prepare(
                task_id="task-001",
                phase="execution",
                target_agent_id="developer",
                catalog=normalized,
                inbox_root=inbox_root,
                id_factory=lambda: "assignment-001",
            )

            record = json.loads((tasks.root / "task-001" / "assignments" / "assignment-001" / "assignment.json").read_text())
            brief = inbox_root / "developer" / "assignment-001" / "TASK.md"

            self.assertEqual(assignment.status, "prepared")
            self.assertEqual(record["target_bot_user_id"], normalized["agents"]["developer"]["discord"]["bot_user_id"])
            self.assertEqual(record["channel_id"], normalized["agents"]["developer"]["discord"]["work_channel_id"])
            self.assertEqual(record["subject"], {"kind": "task", "task_id": "task-001"})
            self.assertTrue(brief.is_file())
            self.assertNotIn(str(root), brief.read_text())
            self.assertFalse((Path(record["host_checkout_path"]) / "TASK.md").exists())

    def test_prepare_execution_rejects_a_reviewer_and_does_not_create_assignment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            normalized, tasks, inbox_root = self.fixture(root)
            (inbox_root / "reviewer").mkdir()
            with self.assertRaisesRegex(AssignmentError, "developer"):
                AssignmentStore(tasks.root).prepare(
                    task_id="task-001",
                    phase="execution",
                    target_agent_id="reviewer",
                    catalog=normalized,
                    inbox_root=inbox_root,
                )
            self.assertFalse((tasks.root / "task-001" / "assignments").exists())

    def test_prepare_review_keeps_delivery_owner_and_binds_reviewer_to_exact_subject(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            normalized, tasks, inbox_root = self.fixture(root)
            tasks.transition("task-001", "active", actor="leader")
            tasks.transition("task-001", "review", actor="leader")
            reviewer = normalized["agents"]["reviewer"]
            reviewer_checkout = Path(reviewer["worktree"]["path"]) / "team-a/service-x"
            reviewer_checkout.mkdir(parents=True)
            (inbox_root / "reviewer").mkdir()
            sha = "a" * 40
            assignment = AssignmentStore(tasks.root).prepare(
                task_id="task-001", phase="review", target_agent_id="reviewer",
                catalog=normalized, inbox_root=inbox_root,
                subject={
                    "kind": "git_commit", "repository": tasks.get("task-001").repository,
                    "base_ref": "origin/develop", "base_sha": "b" * 40,
                    "commit_sha": sha, "review_ref": f"refs/oab/reviews/task-001/{sha}",
                },
                id_factory=lambda: "review-001",
            )
            self.assertEqual(assignment.target_role, "reviewer")
            self.assertEqual(assignment.subject["commit_sha"], sha)
            self.assertEqual(tasks.get("task-001").agent_id, "developer")
            brief = (inbox_root / "reviewer" / "review-001" / "TASK.md").read_text()
            self.assertIn(sha, brief)


if __name__ == "__main__":
    unittest.main()
