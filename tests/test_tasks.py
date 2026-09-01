from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from oab_control.tasks import Task, TaskError, TaskStore


def task(*, kind: str = "code", authorizations: bool = True) -> Task:
    return Task(
        task_id="task-001",
        kind=kind,
        goal="Implement the bounded change",
        scope="Only the exact repository and checkout are in scope.",
        canonical_sources=("docs/spec.md",),
        agent_id="developer",
        repository="/srv/repositories/team-a/service-x",
        checkout_subpath="team-a/service-x",
        worktree_path="/srv/worktrees/developer",
        container_mount_path="/workspaces/developer/team-a/service-x",
        branch="task/task-001",
        base_branch="origin/develop",
        delivery_owner="developer",
        gitlab_identity_ref="gitlab-bootstrap",
        tests=("python -m unittest",),
        completion_marker="tests green",
        checkpoint="report test output",
        deadline="2026-08-31T23:59:00Z",
        reply_to="discord:developer-private",
        commit_authorized=authorizations,
        push_authorized=authorizations,
        mr_authorized=authorizations,
    )


class TaskStoreTests(unittest.TestCase):
    def test_create_persists_brief_envelope_and_leader_owned_transition(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory) / "tasks")
            created = store.create(task(), actor="leader")
            assigned = store.transition(created.task_id, "assigned", actor="leader")
            active = store.transition(created.task_id, "active", actor="leader")
            store.write_report(created.task_id, "Tests are ready.\n", actor="leader")
            brief = (Path(directory) / "tasks" / "task-001" / "brief.md").read_text(encoding="utf-8")
            envelope = active.envelope()
        self.assertEqual(assigned.state, "assigned")
        self.assertEqual(active.state, "active")
        self.assertIn("origin/develop", brief)
        self.assertEqual(envelope["checkout_subpath"], "team-a/service-x")
        self.assertEqual(envelope["reply_to"], "discord:developer-private")

    def test_invalid_actor_transition_and_second_active_task_fail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory) / "tasks")
            store.create(task(), actor="leader")
            with self.assertRaises(TaskError):
                store.transition("task-001", "assigned", actor="developer")
            with self.assertRaises(TaskError):
                store.create(Task(**{**task().as_dict(), "task_id": "task-002"}), actor="leader")
            with self.assertRaises(TaskError):
                store.transition("task-001", "review", actor="leader")

    def test_global_active_task_limit_defaults_to_two_and_is_configurable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory) / "tasks")
            store.create(task(), actor="leader")
            store.create(Task(**{**task().as_dict(), "task_id": "task-002", "agent_id": "researcher", "delivery_owner": "researcher"}), actor="leader")
            with self.assertRaisesRegex(TaskError, "active task limit reached: 2"):
                store.create(Task(**{**task().as_dict(), "task_id": "task-003", "agent_id": "reviewer", "delivery_owner": "reviewer"}), actor="leader")
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory) / "tasks", max_active_tasks=3)
            for task_id, agent_id in (("task-001", "developer"), ("task-002", "researcher"), ("task-003", "reviewer")):
                store.create(Task(**{**task().as_dict(), "task_id": task_id, "agent_id": agent_id, "delivery_owner": agent_id}), actor="leader")
            self.assertEqual(len(store.list()), 3)
        with self.assertRaisesRegex(TaskError, "positive integer"):
            TaskStore("/tmp/tasks", max_active_tasks=0)

    def test_code_acceptance_requires_all_evidence_authorization_and_review(self) -> None:
        evidence = {
            "developer_tests": "run-123",
            "independent_review": "review-123",
            "ci_success": "pipeline-123",
            "ci_status": "success",
            "leader_summary": "accepted scope",
            "human_merge_authorized": True,
            "authorization_actor": "operator",
            "authorization_at": "2026-08-31T12:00:00Z",
            "authorization_scope": "merge !123 after CI",
            "commit_sha": "0123456789abcdef0123456789abcdef01234567",
            "push_ref": "origin/task/task-001",
            "merge_request": "https://gitlab.example.invalid/team-a/service-x/-/merge_requests/123",
            "delivery_repository": "/srv/repositories/team-a/service-x",
            "delivery_branch": "task/task-001",
            "delivery_owner": "developer",
            "nested": {"api_token": "super-secret-value", "note": "redacted"},
        }
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory) / "tasks")
            store.create(task(), actor="leader")
            store.transition("task-001", "assigned", actor="leader")
            store.transition("task-001", "active", actor="leader")
            store.transition("task-001", "review", actor="leader")
            store.write_review("task-001", "Independent review passed.", actor="leader")
            accepted = store.transition("task-001", "accepted", actor="leader", evidence=evidence)
            acceptance = (Path(directory) / "tasks" / "task-001" / "acceptance.md").read_text(encoding="utf-8")
            with self.assertRaisesRegex(TaskError, "agent already owns an active task"):
                store.create(Task(**{**task().as_dict(), "task_id": "task-002"}), actor="leader")
            with self.assertRaisesRegex(TaskError, "merge completion"):
                store.transition("task-001", "closed", actor="leader")
            closed = store.transition(
                "task-001",
                "closed",
                actor="leader",
                evidence={
                    "merge_completed": True,
                    "merge_request": "https://gitlab.example.invalid/team-a/service-x/-/merge_requests/123",
                    "merge_target_branch": "origin/develop",
                    "merge_actor": "operator",
                    "merge_at": "2026-08-31T13:00:00Z",
                },
            )
            delivery = (Path(directory) / "tasks" / "task-001" / "delivery.md").read_text(encoding="utf-8")
        self.assertEqual(accepted.state, "accepted")
        self.assertEqual(closed.state, "closed")
        self.assertTrue(closed.merge_completed)
        self.assertEqual(closed.merge_target_branch, "origin/develop")
        self.assertEqual(closed.closure_kind, "merged")
        self.assertEqual(accepted.delivery_commit, "0123456789abcdef0123456789abcdef01234567")
        self.assertEqual(accepted.delivery_push_ref, "origin/task/task-001")
        self.assertEqual(accepted.delivery_merge_request, "https://gitlab.example.invalid/team-a/service-x/-/merge_requests/123")
        self.assertEqual(accepted.delivery_ci_status, "success")
        self.assertNotIn("super-secret-value", acceptance)
        self.assertIn("[REDACTED]", acceptance)
        self.assertIn("merge_completed: true", delivery)

    def test_code_acceptance_rejects_missing_evidence_and_read_only_task_does_not_require_mr(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory) / "tasks")
            store.create(task(), actor="leader")
            for state in ("assigned", "active", "review"):
                store.transition("task-001", state, actor="leader")
            with self.assertRaises(TaskError):
                store.transition("task-001", "accepted", actor="leader", evidence={})

            research = Task(**{**task(kind="research", authorizations=False).as_dict(), "task_id": "task-002", "agent_id": "researcher"})
            store.create(research, actor="leader")
            for state in ("assigned", "active", "review"):
                store.transition("task-002", state, actor="leader")
            accepted = store.transition(
                "task-002",
                "accepted",
                actor="leader",
                evidence={"traceable_sources": "paper.md", "independent_review": "review-2", "leader_summary": "done"},
            )
            closed = store.transition("task-002", "closed", actor="leader")
        self.assertEqual(accepted.state, "accepted")
        self.assertEqual(closed.state, "closed")
        self.assertEqual(closed.closure_kind, "accepted")

    def test_code_acceptance_rejects_stale_or_wrong_delivery_evidence(self) -> None:
        evidence = {
            "developer_tests": "run-123",
            "independent_review": "review-123",
            "ci_success": "pipeline-123",
            "ci_status": "failed",
            "leader_summary": "accepted scope",
            "human_merge_authorized": True,
            "authorization_actor": "operator",
            "authorization_at": "2026-08-31T12:00:00Z",
            "authorization_scope": "merge !123",
            "commit_sha": "0123456789abcdef0123456789abcdef01234567",
            "push_ref": "origin/task/task-001",
            "merge_request": "!123",
            "delivery_repository": "/srv/repositories/team-a/service-x",
            "delivery_branch": "task/old-branch",
            "delivery_owner": "developer",
        }
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory) / "tasks")
            store.create(task(), actor="leader")
            for state in ("assigned", "active", "review"):
                store.transition("task-001", state, actor="leader")
            with self.assertRaisesRegex(TaskError, "delivery evidence"):
                store.transition("task-001", "accepted", actor="leader", evidence=evidence)

    def test_reports_redact_and_reconciliation_cannot_skip_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory) / "tasks")
            store.create(task(), actor="leader")
            store.transition("task-001", "assigned", actor="leader")
            store.transition("task-001", "active", actor="leader")
            store.transition("task-001", "needs-reconciliation", actor="leader")
            with self.assertRaises(TaskError):
                store.transition("task-001", "closed", actor="leader")
            with self.assertRaises(TaskError):
                store.write_report("task-001", "token=glpat-123456789012345678", actor="leader")

    def test_authorization_flags_are_real_booleans(self) -> None:
        with self.assertRaises(TaskError):
            Task(**{**task().as_dict(), "commit_authorized": "false"})

    def test_cancellation_closes_with_a_persistent_decision_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory) / "tasks")
            store.create(task(), actor="leader")
            closed = store.transition(
                "task-001",
                "closed",
                actor="leader",
                evidence={"cancellation_reason": "superseded", "cancellation_decider": "operator"},
            )
            record = (Path(directory) / "tasks" / "task-001" / "cancellation.md").read_text(encoding="utf-8")
        self.assertEqual(closed.state, "closed")
        self.assertEqual(closed.closure_kind, "cancelled")
        self.assertEqual(closed.cancellation_reason, "superseded")
        self.assertEqual(closed.cancellation_decider, "operator")
        self.assertTrue(closed.cancellation_at.endswith("Z"))
        self.assertIn("superseded", record)

    def test_accepted_code_task_can_be_explicitly_cancelled_before_merge(self) -> None:
        evidence = {
            "developer_tests": "run-123",
            "independent_review": "review-123",
            "ci_success": "pipeline-123",
            "ci_status": "success",
            "leader_summary": "accepted scope",
            "human_merge_authorized": True,
            "authorization_actor": "operator",
            "authorization_at": "2026-08-31T12:00:00Z",
            "authorization_scope": "merge !123 after CI",
            "commit_sha": "0123456789abcdef0123456789abcdef01234567",
            "push_ref": "origin/task/task-001",
            "merge_request": "https://gitlab.example.invalid/team-a/service-x/-/merge_requests/123",
            "delivery_repository": "/srv/repositories/team-a/service-x",
            "delivery_branch": "task/task-001",
            "delivery_owner": "developer",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tasks"
            store = TaskStore(root)
            store.create(task(), actor="leader")
            for state in ("assigned", "active", "review"):
                store.transition("task-001", state, actor="leader")
            store.transition("task-001", "accepted", actor="leader", evidence=evidence)
            closed = store.transition(
                "task-001",
                "closed",
                actor="leader",
                evidence={"cancellation_reason": "MR rejected by operator", "cancellation_decider": "operator"},
            )
            cancellation = (root / "task-001" / "cancellation.md").read_text(encoding="utf-8")
            delivery = (root / "task-001" / "delivery.md").read_text(encoding="utf-8")
        self.assertEqual(closed.closure_kind, "cancelled")
        self.assertFalse(closed.merge_completed)
        self.assertEqual(closed.cancellation_decider, "operator")
        self.assertIn("MR rejected by operator", cancellation)
        self.assertIn("尚未完成 merge", delivery)

    def test_cancelled_task_record_requires_structured_evidence(self) -> None:
        with self.assertRaisesRegex(TaskError, "cancelled closure"):
            Task(**{**task().as_dict(), "state": "closed", "closure_kind": "cancelled"})
        with self.assertRaisesRegex(TaskError, "closure kind"):
            Task(**{**task().as_dict(), "state": "closed"})

    def test_task_fields_reject_credential_shaped_values(self) -> None:
        with self.assertRaises(TaskError):
            Task(**{**task().as_dict(), "goal": "use token=glpat-123456789012345678"})


if __name__ == "__main__":
    unittest.main()
