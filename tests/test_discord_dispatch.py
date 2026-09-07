from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from oab_control.assignments import AssignmentStore
from oab_control.discord_dispatch import DispatchError, dispatch_assignment, reconcile_assignment
from test_assignments import AssignmentStoreTests


class DiscordDispatchTests(unittest.TestCase):
    def prepared(self, root: Path):
        normalized, tasks, inbox_root = AssignmentStoreTests().fixture(root)
        assignment = AssignmentStore(tasks.root).prepare(
            task_id="task-001", phase="execution", target_agent_id="developer",
            catalog=normalized, inbox_root=inbox_root, id_factory=lambda: "assignment-001",
        )
        return normalized, tasks, assignment

    def test_preview_has_no_side_effect_and_confirmed_dispatch_records_message(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            normalized, tasks, assignment = self.prepared(Path(directory))
            calls: list[tuple[str, str]] = []
            preview = dispatch_assignment(
                store=AssignmentStore(tasks.root), assignment_id=assignment.assignment_id,
                catalog=normalized, confirmed=False,
                send=lambda channel, content: calls.append((channel, content)) or "message-1",
                nonce_factory=lambda: "nonce-preview",
            )
            self.assertEqual(calls, [])
            self.assertFalse((tasks.root / "task-001" / "assignments" / "assignment-001" / "dispatch-attempts.jsonl").exists())
            self.assertIn("<@", preview["message"])
            self.assertNotIn("Implement", preview["message"])

            result = dispatch_assignment(
                store=AssignmentStore(tasks.root), assignment_id=assignment.assignment_id,
                catalog=normalized, confirmed=True,
                send=lambda channel, content: calls.append((channel, content)) or "message-1",
                nonce_factory=lambda: "nonce-delivered",
            )
            attempt = json.loads((tasks.root / "task-001" / "assignments" / "assignment-001" / "dispatch-attempts.jsonl").read_text().splitlines()[-1])
            self.assertEqual(len(calls), 1)
            self.assertEqual(result["state"], "delivered")
            self.assertEqual(attempt["message_id"], "message-1")
            self.assertEqual(AssignmentStore(tasks.root).get("task-001", "assignment-001").status, "dispatched")
            self.assertEqual(TaskStoreState(tasks.root, "task-001"), "assigned")

    def test_timeout_becomes_unknown_and_blocks_resend(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            normalized, tasks, assignment = self.prepared(Path(directory))
            def timeout(_channel: str, _content: str) -> str:
                raise TimeoutError("provider response may contain secrets")
            with self.assertRaisesRegex(DispatchError, "unknown"):
                dispatch_assignment(store=AssignmentStore(tasks.root), assignment_id=assignment.assignment_id, catalog=normalized, confirmed=True, send=timeout)
            self.assertEqual(AssignmentStore(tasks.root).get("task-001", "assignment-001").status, "needs_reconciliation")
            with self.assertRaisesRegex(DispatchError, "reconciliation"):
                dispatch_assignment(store=AssignmentStore(tasks.root), assignment_id=assignment.assignment_id, catalog=normalized, confirmed=True, send=lambda *_: "message-2")

    def test_unknown_attempt_reconciles_by_unique_leader_nonce(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            normalized, tasks, assignment = self.prepared(Path(directory))
            with self.assertRaises(DispatchError):
                dispatch_assignment(store=AssignmentStore(tasks.root), assignment_id=assignment.assignment_id,
                                    catalog=normalized, confirmed=True, send=lambda *_: (_ for _ in ()).throw(TimeoutError()),
                                    nonce_factory=lambda: "nonce-unknown")
            leader_bot = normalized["agents"]["leader"]["discord"]["bot_user_id"]
            result = reconcile_assignment(
                store=AssignmentStore(tasks.root), assignment_id=assignment.assignment_id, catalog=normalized,
                find_messages=lambda channel, nonce: [{"id": "message-recovered", "author_id": leader_bot, "thread_id": ""}],
            )
            self.assertEqual(result["state"], "delivered")
            self.assertEqual(result["message_id"], "message-recovered")
            self.assertEqual(AssignmentStore(tasks.root).find(assignment.assignment_id).status, "dispatched")


def TaskStoreState(root: Path, task_id: str) -> str:
    return json.loads((root / task_id / "task.json").read_text())["state"]


if __name__ == "__main__":
    unittest.main()
