from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from oab_control.registry import RegistryError, WorkspaceRecord, WorkspaceRegistry


def record(agent: str, *, task: str = "", status: str = "ready") -> WorkspaceRecord:
    return WorkspaceRecord(
        workspace_id=f"workspace-{agent}",
        owner_agent=agent,
        project_scope="team-a/service-x",
        worktree_path=f"/srv/oab-agent-worktrees/{agent}",
        current_task_id=task,
        current_branch=f"task/{task}" if task else "",
        status=status,
    )


class RegistryTests(unittest.TestCase):
    def test_json_and_markdown_are_projections_of_same_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = WorkspaceRegistry(root / "registry.json", root / "workspace-registry.md")
            registry.save([record("developer", task="task-001", status="active"), record("reviewer")])
            loaded = registry.load()
            markdown = (root / "workspace-registry.md").read_text(encoding="utf-8")
        self.assertEqual([item.workspace_id for item in loaded], ["workspace-developer", "workspace-reviewer"])
        self.assertIn("workspace-developer", markdown)
        self.assertIn("task-001", markdown)
        self.assertIn("| workspace-reviewer |", markdown)
        self.assertIn("工作區索引", markdown)
        self.assertIn("擁有 Agent", markdown)

    def test_upsert_preserves_one_row_per_agent_and_retirement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = WorkspaceRegistry(root / "registry.json", root / "workspace-registry.md")
            registry.save([record("developer")])
            registry.upsert(record("developer", task="task-002", status="active"))
            registry.retire("workspace-developer", record="2026-08-31 operator approved")
            loaded = registry.load()
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].status, "retired")
        self.assertEqual(loaded[0].current_task_id, "")
        self.assertIn("operator approved", loaded[0].retirement_record)

    def test_duplicate_owner_path_and_invalid_retired_state_fail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = WorkspaceRegistry(root / "registry.json", root / "workspace-registry.md")
            with self.assertRaises(RegistryError):
                registry.save([record("developer"), record("developer")])
            with self.assertRaises(RegistryError):
                registry.save([record("developer"), WorkspaceRecord(**{**record("reviewer").as_dict(), "worktree_path": "/srv/oab-agent-worktrees/developer"})])
            with self.assertRaises(RegistryError):
                registry.save([WorkspaceRecord(**{**record("reviewer").as_dict(), "status": "retired", "current_task_id": "task-003"})])


if __name__ == "__main__":
    unittest.main()
