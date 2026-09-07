from pathlib import Path
import tempfile
import unittest

from oab_control.artifacts import ArtifactError, snapshot_task_artifact


class ArtifactTests(unittest.TestCase):
    def test_snapshot_is_digest_bound_and_outside_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tasks" / "task-1"
            root.mkdir(parents=True)
            (root / "report.md").write_text("report", encoding="utf-8")
            subject = snapshot_task_artifact(root.parent, "task-1")
            self.assertEqual(len(subject["sha256"]), 64)
            self.assertEqual(Path(subject["snapshot_path"]).read_text(), "report")
            self.assertTrue(Path(subject["snapshot_path"]).is_relative_to(root / "artifacts"))

    def test_arbitrary_path_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tasks" / "task-1"
            root.mkdir(parents=True)
            with self.assertRaises(ArtifactError):
                snapshot_task_artifact(root.parent.parent, "task-1", "../secret")
