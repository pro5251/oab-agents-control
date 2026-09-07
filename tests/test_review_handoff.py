from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest

from oab_control.review_handoff import HandoffError, prepare_code_review


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, text=True, capture_output=True).stdout.strip()


class ReviewHandoffTests(unittest.TestCase):
    def fixture(self, root: Path) -> tuple[Path, Path, Path, str]:
        source = root / "developer"
        mirror = root / "delivery.git"
        reviewer = root / "reviewer"
        git(root, "init", "--bare", str(mirror))
        git(root, "init", "-b", "task/task-001", str(source))
        git(source, "config", "user.email", "developer@example.invalid")
        git(source, "config", "user.name", "Developer")
        (source / "value.txt").write_text("base\n")
        git(source, "add", "value.txt")
        git(source, "commit", "-m", "base")
        base = git(source, "rev-parse", "HEAD")
        (source / "value.txt").write_text("review me\n")
        git(source, "commit", "-am", "delivery")
        git(root, "clone", str(mirror), str(reviewer))
        return source, mirror, reviewer, base

    def test_exact_commit_is_published_only_to_namespaced_ref_and_checked_out_detached(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source, mirror, reviewer, base = self.fixture(Path(directory))
            subject = prepare_code_review(
                source_checkout=source,
                reviewer_checkout=reviewer,
                mirror=mirror,
                task_id="task-001",
                expected_branch="task/task-001",
                base_sha=base,
            )
            self.assertEqual(git(reviewer, "rev-parse", "HEAD"), subject["commit_sha"])
            self.assertEqual(git(reviewer, "branch", "--show-current"), "")
            self.assertEqual(git(mirror, "show-ref", "--hash", subject["review_ref"]), subject["commit_sha"])
            heads = subprocess.run(["git", "for-each-ref", "--format=%(refname)", "refs/heads"], cwd=mirror, check=True, text=True, capture_output=True).stdout
            self.assertEqual(heads, "")

    def test_dirty_developer_checkout_is_rejected_before_publishing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source, mirror, reviewer, base = self.fixture(Path(directory))
            (source / "untracked.txt").write_text("dirty")
            with self.assertRaisesRegex(HandoffError, "clean"):
                prepare_code_review(source_checkout=source, reviewer_checkout=reviewer, mirror=mirror, task_id="task-001", expected_branch="task/task-001", base_sha=base)
            self.assertEqual(git(mirror, "for-each-ref", "--format=%(refname)", "refs/oab/reviews"), "")


if __name__ == "__main__":
    unittest.main()
