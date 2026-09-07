from pathlib import Path
import tempfile
import unittest

from oab_control.assignments import AssignmentStore, AssignmentError
from test_assignments import AssignmentStoreTests


class PruneTests(unittest.TestCase):
    def test_prune_is_explicit_and_keeps_review_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            normalized, tasks, inbox = AssignmentStoreTests().fixture(root)
            assignment = AssignmentStore(tasks.root).prepare(task_id="task-001", phase="execution", target_agent_id="developer", catalog=normalized, inbox_root=inbox, id_factory=lambda: "assignment-1")
            tasks.transition("task-001", "active", actor="leader")
            tasks.transition("task-001", "review", actor="leader")
            tasks.transition("task-001", "active", actor="leader")
            tasks.transition("task-001", "closed", actor="leader", evidence={"cancellation_reason": "done", "cancellation_decider": "operator"})
            store = AssignmentStore(tasks.root)
            preview = store.prune(assignment, inbox_root=inbox)
            result = store.prune(assignment, inbox_root=inbox, confirmed=True)
            self.assertFalse(preview["pruned"])
            self.assertTrue(result["pruned"])
            self.assertFalse((inbox / "developer" / "assignment-1").exists())

    def test_prune_rejects_open_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            normalized, tasks, inbox = AssignmentStoreTests().fixture(root)
            assignment = AssignmentStore(tasks.root).prepare(task_id="task-001", phase="execution", target_agent_id="developer", catalog=normalized, inbox_root=inbox, id_factory=lambda: "assignment-1")
            with self.assertRaisesRegex(AssignmentError, "closed"):
                AssignmentStore(tasks.root).prune(assignment, inbox_root=inbox, confirmed=True)
