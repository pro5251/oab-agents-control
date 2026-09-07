from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from oab_control.assignments import AssignmentStore
from oab_control.discord_dispatch import dispatch_assignment
from oab_control.discord_receipts import ReceiptError, sync_assignment
from test_assignments import AssignmentStoreTests


class DiscordReceiptTests(unittest.TestCase):
    def prepared_review(self, root: Path):
        normalized, tasks, inbox_root = AssignmentStoreTests().fixture(root)
        tasks.transition("task-001", "active", actor="leader")
        tasks.transition("task-001", "review", actor="leader")
        reviewer = normalized["agents"]["reviewer"]
        (Path(reviewer["worktree"]["path"]) / "team-a/service-x").mkdir(parents=True)
        (inbox_root / "reviewer").mkdir()
        sha = "a" * 40
        assignment = AssignmentStore(tasks.root).prepare(
            task_id="task-001", phase="review", target_agent_id="reviewer", catalog=normalized,
            inbox_root=inbox_root, id_factory=lambda: "review-001",
            subject={"kind": "git_commit", "repository": tasks.get("task-001").repository,
                     "base_ref": "origin/develop", "base_sha": "b" * 40, "commit_sha": sha,
                     "review_ref": f"refs/oab/reviews/task-001/{sha}"},
        )
        dispatch_assignment(store=AssignmentStore(tasks.root), assignment_id=assignment.assignment_id,
                            catalog=normalized, confirmed=True, send=lambda *_: "dispatch-1",
                            nonce_factory=lambda: "nonce-1")
        return normalized, tasks, assignment, sha

    def test_matching_reviewer_thread_response_creates_structured_review_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            normalized, tasks, assignment, sha = self.prepared_review(Path(directory))
            reviewer_bot = normalized["agents"]["reviewer"]["discord"]["bot_user_id"]
            record = sync_assignment(
                store=AssignmentStore(tasks.root), assignment_id=assignment.assignment_id, catalog=normalized,
                discover_thread=lambda channel, message: "thread-1",
                read_messages=lambda thread: [{
                    "id": "response-1", "channel_id": thread, "author_id": reviewer_bot,
                    "timestamp": "2099-01-01T00:00:00Z",
                    "content": f"assignment: review-001\nsubject: {sha}\nverdict: passed\nfindings: No blocking findings.",
                }],
            )
            self.assertEqual(record["verdict"], "passed")
            self.assertEqual(record["reviewer_bot_user_id"], reviewer_bot)
            self.assertEqual(record["thread_id"], "thread-1")
            self.assertEqual(AssignmentStore(tasks.root).get("task-001", "review-001").status, "completed")

    def test_wrong_author_cannot_create_review_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            normalized, tasks, assignment, sha = self.prepared_review(Path(directory))
            with self.assertRaisesRegex(ReceiptError, "matching"):
                sync_assignment(
                    store=AssignmentStore(tasks.root), assignment_id=assignment.assignment_id, catalog=normalized,
                    discover_thread=lambda *_: "thread-1",
                    read_messages=lambda thread: [{"id": "fake", "channel_id": thread, "author_id": "99999999999999999", "timestamp": "2099-01-01T00:00:00Z", "content": f"assignment: review-001\nsubject: {sha}\nverdict: passed\nfindings: fake"}],
                )
            self.assertIsNone(AssignmentStore(tasks.root).review_record(assignment))


if __name__ == "__main__":
    unittest.main()
