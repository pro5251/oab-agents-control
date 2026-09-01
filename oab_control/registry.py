"""Durable workspace registry with one source model and two projections."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import tempfile
from typing import Iterable, Mapping


class RegistryError(ValueError):
    """Raised when a registry update would make workspace state ambiguous."""


@dataclass(frozen=True)
class WorkspaceRecord:
    workspace_id: str
    owner_agent: str
    project_scope: str
    worktree_path: str
    default_branch: str = "origin/develop"
    current_task_id: str = ""
    current_branch: str = ""
    current_mr: str = ""
    status: str = "ready"
    retirement_record: str = ""

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


def _escape(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


class WorkspaceRegistry:
    """Write JSON and Markdown from the same validated record collection."""

    def __init__(self, json_path: str | Path, markdown_path: str | Path):
        self.json_path = Path(json_path)
        self.markdown_path = Path(markdown_path)

    def load(self) -> list[WorkspaceRecord]:
        try:
            document = json.loads(self.json_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, json.JSONDecodeError) as exc:
            raise RegistryError(f"unable to read workspace registry: {type(exc).__name__}") from exc
        if not isinstance(document, dict) or document.get("version") != 1 or not isinstance(document.get("workspaces"), list):
            raise RegistryError("workspace registry must be version 1 with a workspaces list")
        records: list[WorkspaceRecord] = []
        for item in document["workspaces"]:
            if not isinstance(item, Mapping):
                raise RegistryError("workspace record must be a mapping")
            try:
                records.append(WorkspaceRecord(**item))
            except TypeError as exc:
                raise RegistryError("workspace record has an invalid shape") from exc
        self._validate(records)
        return records

    def save(self, records: Iterable[WorkspaceRecord]) -> list[WorkspaceRecord]:
        normalized = sorted(records, key=lambda record: record.workspace_id)
        self._validate(normalized)
        document = {"version": 1, "workspaces": [record.as_dict() for record in normalized]}
        self._atomic_write(self.json_path, json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        self._atomic_write(self.markdown_path, self._render_markdown(normalized))
        return normalized

    def upsert(self, record: WorkspaceRecord) -> list[WorkspaceRecord]:
        records = [existing for existing in self.load() if existing.workspace_id != record.workspace_id]
        records.append(record)
        return self.save(records)

    def retire(self, workspace_id: str, *, record: str) -> list[WorkspaceRecord]:
        records = self.load()
        for index, existing in enumerate(records):
            if existing.workspace_id == workspace_id:
                records[index] = WorkspaceRecord(**{**existing.as_dict(), "status": "retired", "current_task_id": "", "current_branch": "", "current_mr": "", "retirement_record": record})
                return self.save(records)
        raise RegistryError(f"unknown workspace: {workspace_id}")

    def clear_task(self, workspace_id: str) -> list[WorkspaceRecord]:
        """Clear task/branch/MR fields after a verified task cleanup."""

        records = self.load()
        for index, existing in enumerate(records):
            if existing.workspace_id == workspace_id:
                if existing.status == "retired":
                    raise RegistryError("retired workspace cannot become active again")
                records[index] = WorkspaceRecord(
                    **{
                        **existing.as_dict(),
                        "current_task_id": "",
                        "current_branch": "",
                        "current_mr": "",
                        "status": "ready",
                    }
                )
                return self.save(records)
        raise RegistryError(f"unknown workspace: {workspace_id}")

    @staticmethod
    def _validate(records: list[WorkspaceRecord]) -> None:
        workspace_ids: set[str] = set()
        agents: set[str] = set()
        paths: set[str] = set()
        valid_statuses = {"ready", "active", "retired"}
        for record in records:
            if record.workspace_id in workspace_ids:
                raise RegistryError(f"duplicate workspace ID: {record.workspace_id}")
            if record.owner_agent in agents:
                raise RegistryError(f"an agent has more than one workspace: {record.owner_agent}")
            if record.worktree_path in paths:
                raise RegistryError(f"duplicate worktree path: {record.worktree_path}")
            if record.status not in valid_statuses:
                raise RegistryError(f"invalid workspace status: {record.status}")
            if not Path(record.worktree_path).is_absolute() or any(part in {".", ".."} for part in Path(record.worktree_path).parts):
                raise RegistryError("worktree path must be an explicit absolute path")
            if not record.owner_agent or not record.project_scope:
                raise RegistryError("workspace owner and project scope are required")
            if record.status == "retired" and record.current_task_id:
                raise RegistryError("retired workspace cannot have an active task")
            workspace_ids.add(record.workspace_id)
            agents.add(record.owner_agent)
            paths.add(record.worktree_path)

    @staticmethod
    def _render_markdown(records: list[WorkspaceRecord]) -> str:
        lines = [
            "# Workspace Registry 工作區索引",
            "",
            "由版本化的本機 registry model 產生。Agent 可讀取此 projection；只有 leader／control process 可以寫入。",
            "",
            "| Workspace ID | 擁有 Agent | 專案範圍 | Worktree 路徑 | 預設 Branch | 目前 Task ID | 目前 Branch | 目前 MR | 狀態 | 退役紀錄 |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        for record in records:
            values = [
                record.workspace_id,
                record.owner_agent,
                record.project_scope,
                record.worktree_path,
                record.default_branch,
                record.current_task_id or "_none_",
                record.current_branch or "_none_",
                record.current_mr or "_none_",
                record.status,
                record.retirement_record or "_none_",
            ]
            lines.append("| " + " | ".join(_escape(value) for value in values) + " |")
        lines.extend(
            [
                "",
                "規則：每個 Agent 擁有一個持久 worktree，且最多只能有一個 active task；任務完成會清除 task／branch／MR，但不移除 worktree；退役時保留此列紀錄。",
                "",
            ]
        )
        return "\n".join(lines)

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except OSError as exc:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise RegistryError(f"unable to write registry: {type(exc).__name__}") from exc
