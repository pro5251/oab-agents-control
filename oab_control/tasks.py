"""Leader-owned durable task records and explicit acceptance gates."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Iterable, Mapping


class TaskError(ValueError):
    """Raised when a task mutation violates the lifecycle contract."""


TASK_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
CREDENTIAL = re.compile(r"(?:glpat-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{16,}|sk-[A-Za-z0-9_-]{16,}|xox[baprs]-[A-Za-z0-9_-]{16,}|(?:https?|ssh)://[^/\s:@]+:[^@\s]+@|-----BEGIN [A-Z ]*PRIVATE KEY-----|(?:token|password|secret)\s*[:=])", re.I)
STATES = {"planned", "assigned", "active", "review", "accepted", "closed", "needs-reconciliation"}
TERMINAL = {"closed"}
ACTIVE = {"planned", "assigned", "active", "review", "accepted", "needs-reconciliation"}
DEFAULT_MAX_ACTIVE_TASKS = 2
TRANSITIONS = {
    "planned": {"assigned", "needs-reconciliation", "closed"},
    "assigned": {"active", "needs-reconciliation", "closed"},
    "active": {"review", "needs-reconciliation", "closed"},
    "review": {"accepted", "active", "needs-reconciliation", "closed"},
    "accepted": {"closed"},
    "needs-reconciliation": {"assigned", "active", "closed"},
    "closed": set(),
}


@dataclass(frozen=True)
class Task:
    task_id: str
    kind: str
    goal: str
    scope: str
    canonical_sources: tuple[str, ...]
    agent_id: str
    repository: str
    checkout_subpath: str
    worktree_path: str
    container_mount_path: str
    branch: str
    base_branch: str
    delivery_owner: str
    gitlab_identity_ref: str
    tests: tuple[str, ...]
    completion_marker: str
    checkpoint: str
    deadline: str
    reply_to: str
    commit_authorized: bool = False
    push_authorized: bool = False
    mr_authorized: bool = False
    delivery_commit: str = ""
    delivery_push_ref: str = ""
    delivery_merge_request: str = ""
    delivery_ci_status: str = ""
    merge_completed: bool = False
    merge_request: str = ""
    merge_target_branch: str = ""
    merge_actor: str = ""
    merge_at: str = ""
    closure_kind: str = ""
    cancellation_reason: str = ""
    cancellation_decider: str = ""
    cancellation_at: str = ""
    state: str = "planned"
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self) -> None:
        if not TASK_ID.fullmatch(self.task_id):
            raise TaskError("unsafe task ID")
        if self.kind not in {"code", "research", "document", "architecture"}:
            raise TaskError("task kind must be code, research, document, or architecture")
        if self.state not in STATES:
            raise TaskError("invalid task state")
        if self.closure_kind not in {"", "merged", "accepted", "cancelled"}:
            raise TaskError("invalid task closure kind")
        if self.closure_kind and self.state != "closed":
            raise TaskError("task closure kind requires closed state")
        if self.state == "closed" and not self.closure_kind:
            raise TaskError("closed task requires a closure kind")
        if not self.agent_id or not self.repository or not self.checkout_subpath or not self.worktree_path or not self.container_mount_path or not self.branch or not self.base_branch or not self.delivery_owner or not self.gitlab_identity_ref or not self.deadline or not self.reply_to:
            raise TaskError("task routing and delivery fields cannot be empty")
        if not all(isinstance(value, str) for value in (self.agent_id, self.repository, self.checkout_subpath, self.worktree_path, self.container_mount_path, self.branch, self.base_branch, self.delivery_owner, self.gitlab_identity_ref, self.deadline, self.reply_to)):
            raise TaskError("task routing fields must be strings")
        if not Path(self.repository).is_absolute() or not Path(self.worktree_path).is_absolute() or not Path(self.container_mount_path).is_absolute():
            raise TaskError("task repository, worktree, and container mount paths must be absolute")
        if self.checkout_subpath.startswith("/") or any(part in {"", ".", ".."} for part in self.checkout_subpath.split("/")):
            raise TaskError("task checkout_subpath must be a safe relative path")
        if not BRANCH.fullmatch(self.branch) or self.branch.startswith("/") or self.branch.endswith("/") or ".." in self.branch.split("/"):
            raise TaskError("unsafe task branch")
        if not BRANCH.fullmatch(self.base_branch) or not self.base_branch.startswith("origin/"):
            raise TaskError("task base branch must be an origin remote ref")
        if not self.goal or not self.scope or not self.completion_marker or not self.checkpoint:
            raise TaskError("brief goal, scope, completion marker, and checkpoint are required")
        if any(not isinstance(value, bool) for value in (self.commit_authorized, self.push_authorized, self.mr_authorized, self.merge_completed)):
            raise TaskError("commit, push, MR authorization, and merge completion fields must be booleans")
        if self.merge_completed:
            merge_evidence = {
                "merge_request": self.merge_request,
                "merge_target_branch": self.merge_target_branch,
                "merge_actor": self.merge_actor,
                "merge_at": self.merge_at,
                "merge_completed": self.merge_completed,
            }
            TaskStore._validate_merge_evidence(merge_evidence)
            if self.merge_target_branch != self.base_branch:
                raise TaskError("merge target branch does not match the task base branch")
        if self.closure_kind == "merged":
            if self.kind != "code" or not self.merge_completed:
                raise TaskError("merged closure requires a completed code-task merge")
        if self.closure_kind == "accepted" and self.kind == "code":
            raise TaskError("code tasks cannot close as accepted without merge evidence")
        if self.state == "closed" and self.kind == "code" and self.closure_kind not in {"merged", "cancelled"}:
            raise TaskError("closed code task requires merged or cancelled closure")
        delivery_values = (
            self.delivery_commit,
            self.delivery_push_ref,
            self.delivery_merge_request,
            self.delivery_ci_status,
        )
        if any(not isinstance(value, str) for value in delivery_values):
            raise TaskError("delivery evidence fields must be strings")
        if any(delivery_values) and self.kind != "code":
            raise TaskError("delivery evidence is only valid for code tasks")
        if self.kind == "code" and self.state == "accepted" and not all(value.strip() for value in delivery_values):
            raise TaskError("accepted code task requires structured delivery evidence")
        if self.closure_kind == "merged" and not all(value.strip() for value in delivery_values):
            raise TaskError("merged closure requires structured delivery evidence")
        cancellation_values = (self.cancellation_reason, self.cancellation_decider, self.cancellation_at)
        if any(not isinstance(value, str) for value in cancellation_values):
            raise TaskError("cancellation fields must be strings")
        if self.closure_kind == "cancelled":
            if not all(value.strip() for value in cancellation_values):
                raise TaskError("cancelled closure requires reason, decider, and timestamp")
            _require_timezone(self.cancellation_at, "cancellation_at")
        elif any(cancellation_values):
            raise TaskError("cancellation evidence is only valid for a cancelled closure")
        if any(isinstance(value, str) and CREDENTIAL.search(value) for value in self.as_dict().values()):
            raise TaskError("task fields may contain references only, never credential-shaped values")

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def envelope(self) -> dict[str, Any]:
        """Stable dispatch fields; no credentials are ever copied into it."""

        return {
            "task_id": self.task_id,
            "action": "execute_task",
            "kind": self.kind,
            "repo": self.repository,
            "checkout_subpath": self.checkout_subpath,
            "worktree_path": self.worktree_path,
            "container_mount_path": self.container_mount_path,
            "branch": self.branch,
            "base_branch": self.base_branch,
            "delivery_owner": self.delivery_owner,
            "deadline": self.deadline,
            "reply_to": self.reply_to,
        }


class TaskStore:
    """A small local store whose only writer is the leader/control process."""

    def __init__(
        self,
        root: str | Path,
        *,
        leader_id: str = "leader",
        max_active_tasks: int = DEFAULT_MAX_ACTIVE_TASKS,
    ):
        if not isinstance(max_active_tasks, int) or isinstance(max_active_tasks, bool) or max_active_tasks < 1:
            raise TaskError("max active tasks must be a positive integer")
        self.root = Path(root)
        self.leader_id = leader_id
        self.max_active_tasks = max_active_tasks
        self.events_path = self.root / "events.jsonl"

    def create(self, task: Task, *, actor: str) -> Task:
        self._leader(actor)
        if task.state != "planned":
            raise TaskError("new tasks must start in planned state")
        path = self._task_dir(task.task_id)
        if path.exists():
            raise TaskError(f"task already exists: {task.task_id}")
        active_tasks = [existing for existing in self.list() if existing.state in ACTIVE]
        if len(active_tasks) >= self.max_active_tasks:
            raise TaskError(f"active task limit reached: {self.max_active_tasks}")
        for existing in active_tasks:
            if existing.agent_id == task.agent_id and existing.state in ACTIVE:
                raise TaskError(f"agent already owns an active task: {task.agent_id}")
        now = _now()
        task = Task(**{**task.as_dict(), "created_at": now, "updated_at": now})
        path.mkdir(parents=True)
        self._write_json(path / "task.json", task.as_dict())
        self._write_text(path / "brief.md", self._brief(task))
        self._write_text(path / "report.md", "# Worker 回報\n\n等待 leader 從 private work channel 轉錄。\n")
        self._write_text(path / "review.md", "# 獨立 Review\n\n等待 reviewer 證據。\n")
        self._write_text(path / "acceptance.md", "# 驗收\n\n尚未驗收。\n")
        self._write_text(path / "delivery.md", "# GitLab 交付\n\n尚未完成 merge。\n")
        self._write_text(path / "cancellation.md", "# 取消\n\n尚未取消。\n")
        self._event(task, actor, "created", {"state": task.state})
        return task

    def get(self, task_id: str) -> Task:
        try:
            document = json.loads((self._task_dir(task_id) / "task.json").read_text(encoding="utf-8"))
            document["canonical_sources"] = tuple(document.get("canonical_sources", []))
            document["tests"] = tuple(document.get("tests", []))
            return Task(**document)
        except FileNotFoundError as exc:
            raise TaskError(f"unknown task: {task_id}") from exc
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            raise TaskError(f"invalid task record: {task_id}") from exc

    def list(self) -> list[Task]:
        tasks: list[Task] = []
        for path in sorted(self.root.iterdir() if self.root.exists() else []):
            if path.is_dir() and TASK_ID.fullmatch(path.name) and (path / "task.json").exists():
                tasks.append(self.get(path.name))
        return tasks

    def transition(
        self,
        task_id: str,
        state: str,
        *,
        actor: str,
        evidence: Mapping[str, Any] | None = None,
        verified_fields: Iterable[str] = (),
    ) -> Task:
        self._leader(actor)
        task = self.get(task_id)
        if state not in STATES:
            raise TaskError("invalid target state")
        if state not in TRANSITIONS[task.state]:
            raise TaskError(f"invalid transition: {task.state} -> {state}")
        is_cancellation = state == "closed" and _has_cancellation_evidence(evidence or {})
        if state == "accepted":
            self._require_acceptance(task, evidence or {})
        elif state == "closed" and (task.state != "accepted" or is_cancellation):
            self._require_cancellation(evidence or {})
        elif state == "closed" and task.kind == "code":
            self._require_merge_completion(task, evidence or {})
        updated_fields = {**task.as_dict(), "state": state, "updated_at": _now()}
        if state == "accepted" and task.kind == "code":
            updated_fields.update(
                {
                    "delivery_commit": evidence["commit_sha"],
                    "delivery_push_ref": evidence["push_ref"],
                    "delivery_merge_request": evidence["merge_request"],
                    "delivery_ci_status": evidence["ci_status"],
                }
            )
        elif state == "closed" and is_cancellation:
            updated_fields.update(
                {
                    "closure_kind": "cancelled",
                    "cancellation_reason": evidence["cancellation_reason"],
                    "cancellation_decider": evidence["cancellation_decider"],
                    "cancellation_at": _now(),
                }
            )
        elif state == "closed" and task.state == "accepted" and task.kind == "code":
            updated_fields.update(
                {
                    "merge_completed": evidence["merge_completed"],
                    "merge_request": evidence["merge_request"],
                    "merge_target_branch": evidence["merge_target_branch"],
                    "merge_actor": evidence["merge_actor"],
                    "merge_at": evidence["merge_at"],
                    "closure_kind": "merged",
                }
            )
        elif state == "closed" and task.state == "accepted":
            updated_fields["closure_kind"] = "accepted"
        elif state == "closed":
            updated_fields["closure_kind"] = "cancelled"
        updated = Task(**updated_fields)
        self._write_json(self._task_dir(task_id) / "task.json", updated.as_dict())
        if state == "accepted":
            self._write_text(
                self._task_dir(task_id) / "acceptance.md",
                self._acceptance(updated, evidence or {}, frozenset(verified_fields)),
            )
        elif state == "closed" and is_cancellation:
            self._write_text(self._task_dir(task_id) / "cancellation.md", self._cancellation(updated, evidence or {}))
        elif state == "closed" and task.state == "accepted" and task.kind == "code":
            self._write_text(self._task_dir(task_id) / "delivery.md", self._delivery(updated, evidence or {}))
        elif state == "closed" and task.state != "accepted":
            self._write_text(self._task_dir(task_id) / "cancellation.md", self._cancellation(updated, evidence or {}))
        self._event(updated, actor, "transition", {"from": task.state, "to": state, "evidence": _safe_evidence(evidence or {})})
        return updated

    def write_report(self, task_id: str, content: str, *, actor: str) -> Task:
        self._leader(actor)
        task = self.get(task_id)
        if task.state not in {"active", "review", "needs-reconciliation"}:
            raise TaskError("report can only be recorded for an active/review task")
        self._write_text(self._task_dir(task_id) / "report.md", _safe_markdown(content, "worker report"))
        self._event(task, actor, "report_recorded", {})
        return task

    def write_review(self, task_id: str, content: str, *, actor: str) -> Task:
        self._leader(actor)
        task = self.get(task_id)
        if task.state != "review":
            raise TaskError("review can only be recorded in review state")
        self._write_text(self._task_dir(task_id) / "review.md", _safe_markdown(content, "independent review"))
        self._event(task, actor, "review_recorded", {})
        return task

    def _require_acceptance(self, task: Task, evidence: Mapping[str, Any]) -> None:
        if task.kind == "code":
            required = {
                "developer_tests",
                "independent_review",
                "ci_success",
                "ci_status",
                "leader_summary",
                "human_merge_authorized",
                "authorization_actor",
                "authorization_at",
                "authorization_scope",
                "commit_sha",
                "push_ref",
                "merge_request",
                "delivery_repository",
                "delivery_branch",
                "delivery_owner",
            }
            text_required = required - {"human_merge_authorized"}
            missing = sorted(key for key in text_required if not isinstance(evidence.get(key), str) or not evidence[key].strip())
            if evidence.get("human_merge_authorized") is not True:
                missing = sorted(set(missing) | {"human_merge_authorized"})
            if missing:
                raise TaskError("code acceptance missing: " + ", ".join(missing))
            if not (task.commit_authorized and task.push_authorized and task.mr_authorized):
                raise TaskError("code acceptance requires explicit commit, push, and MR authorization on the task")
            if evidence["delivery_repository"] != task.repository or evidence["delivery_branch"] != task.branch or evidence["delivery_owner"] != task.delivery_owner:
                raise TaskError("code acceptance delivery evidence does not match the task owner, repository, or branch")
            if evidence["ci_status"].lower() != "success":
                raise TaskError("code acceptance requires successful GitLab CI status")
            try:
                authorization_at = datetime.fromisoformat(evidence["authorization_at"].replace("Z", "+00:00"))
            except (TypeError, ValueError) as exc:
                raise TaskError("code acceptance authorization_at must be an ISO-8601 timestamp") from exc
            if authorization_at.tzinfo is None:
                raise TaskError("code acceptance authorization_at must include a timezone")
        else:
            required = {"traceable_sources", "independent_review", "leader_summary"}
            missing = sorted(key for key in required if not evidence.get(key))
            if missing:
                raise TaskError("evidence acceptance missing: " + ", ".join(missing))

    @staticmethod
    def _require_cancellation(evidence: Mapping[str, Any]) -> None:
        required = {"cancellation_reason", "cancellation_decider"}
        missing = sorted(key for key in required if not evidence.get(key))
        if missing:
            raise TaskError("cancellation requires: " + ", ".join(missing))

    @staticmethod
    def _require_merge_completion(task: Task, evidence: Mapping[str, Any]) -> None:
        TaskStore._validate_merge_evidence(evidence)
        if evidence["merge_target_branch"] != task.base_branch:
            raise TaskError("merge target branch does not match the task base branch")

    @staticmethod
    def _validate_merge_evidence(evidence: Mapping[str, Any]) -> None:
        required = {"merge_request", "merge_target_branch", "merge_actor", "merge_at"}
        missing = sorted(key for key in required if not isinstance(evidence.get(key), str) or not evidence[key].strip())
        if evidence.get("merge_completed") is not True:
            missing = sorted(set(missing) | {"merge_completed"})
        if missing:
            raise TaskError("code task close requires merge completion evidence: " + ", ".join(missing))
        _require_timezone(evidence["merge_at"], "merge_at")

    def _leader(self, actor: str) -> None:
        if actor != self.leader_id:
            raise TaskError("only the configured leader may mutate coordination records")

    def _task_dir(self, task_id: str) -> Path:
        if not isinstance(task_id, str) or not TASK_ID.fullmatch(task_id):
            raise TaskError("unsafe task ID")
        return self.root / task_id

    def _event(self, task: Task, actor: str, event: str, details: Mapping[str, Any]) -> None:
        payload = {"at": _now(), "task_id": task.task_id, "actor": actor, "event": event, "details": _safe_evidence(details)}
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        with self.events_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    @staticmethod
    def _brief(task: Task) -> str:
        envelope = json.dumps(task.envelope(), ensure_ascii=False, sort_keys=True, indent=2)
        tests = "\n".join(f"- `{item}`" for item in task.tests)
        sources = "\n".join(f"- `{item}`" for item in task.canonical_sources)
        return f"""# 任務 {task.task_id}

## 目標

{task.goal}

## 範圍與限制

{task.scope}

## 主要來源

{sources or "- 尚未記錄"}

## 路由與交付

- 精確 repository：`{task.repository}`
- Checkout：`{task.checkout_subpath}`
- Agent worktree：`{task.worktree_path}`
- Container mount：`{task.container_mount_path}`
- Branch：`{task.branch}`
- Base branch：`{task.base_branch}`
- Agent：`{task.agent_id}`
- Delivery owner：`{task.delivery_owner}`
- GitLab identity reference：`{task.gitlab_identity_ref}`
- Deadline：`{task.deadline}`
- 回覆 channel：`{task.reply_to}`

## 必要測試

{tests or "- 尚未記錄測試"}

## 完成條件與 checkpoint

- Completion marker：{task.completion_marker}
- Checkpoint：{task.checkpoint}

## 授權範圍

- Commit 已授權：`{str(task.commit_authorized).lower()}`
- Push 已授權：`{str(task.push_authorized).lower()}`
- Merge request 已授權：`{str(task.mr_authorized).lower()}`

程式驗收還會記錄 commit SHA、pushed ref、merge request reference、CI 成功狀態，
以及精確 delivery owner／repository／branch 證據。leader 標記 accepted 前，
會將這些值與此任務比對。

## 派工 Envelope

```json
{envelope}
```
"""

    @staticmethod
    def _acceptance(task: Task, evidence: Mapping[str, Any], verified: frozenset[str] = frozenset()) -> str:
        """Record acceptance evidence with its provenance.

        A field read from the repository and a field someone typed are not the
        same kind of claim, and a reader months later cannot tell them apart
        unless the record says so.  Verified fields can be re-checked against
        the checkout; attested ones can only be believed.
        """

        safe_evidence = _safe_evidence(evidence)

        def render(key: str) -> str:
            value = safe_evidence[key]
            if isinstance(value, bool):
                value = str(value).lower()
            elif isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False, sort_keys=True)
            return f"- {key}: {value}"

        verified_keys = sorted(key for key in safe_evidence if key in verified)
        attested_keys = sorted(key for key in safe_evidence if key not in verified)

        lines = [f"# 驗收：{task.task_id}", "", "狀態：accepted", ""]
        if verified_keys:
            lines += [
                "## 已驗證證據",
                "",
                "由控制平面從該任務的 checkout 直接讀出，未經任何角色轉述。",
                "可回頭重新查證。",
                "",
                *(render(key) for key in verified_keys),
                "",
            ]
        lines += [
            "## 聲稱證據",
            "",
            "由角色宣稱，控制平面無法查證。只能相信當時的判斷。",
            "",
            *(render(key) for key in attested_keys),
            "",
        ]
        if not verified_keys:
            lines += [
                "> ⚠️ 本次驗收沒有任何已驗證證據——所有欄位都是轉述。",
                "> 使用 `task-collect` 或 `--collect` 可讓控制平面直接讀取可查證的欄位。",
                "",
            ]
        return "\n".join(lines)

    @staticmethod
    def _cancellation(task: Task, evidence: Mapping[str, Any]) -> str:
        safe_evidence = _safe_evidence(evidence)
        lines = [f"# 取消：{task.task_id}", "", "狀態：closed", "", "取消紀錄："]
        for key in sorted(safe_evidence):
            value = safe_evidence[key]
            if isinstance(value, bool):
                value = str(value).lower()
            elif isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False, sort_keys=True)
            lines.append(f"- {key}: {value}")
        lines.extend(
            [
                f"- cancellation_at: {task.cancellation_at}",
                "",
            ]
        )
        return "\n".join(lines)

    @staticmethod
    def _delivery(task: Task, evidence: Mapping[str, Any]) -> str:
        safe_evidence = _safe_evidence(evidence)
        lines = [f"# GitLab 交付：{task.task_id}", "", "狀態：closed", "", "Merge 完成證據："]
        for key in sorted(safe_evidence):
            value = safe_evidence[key]
            if isinstance(value, bool):
                value = str(value).lower()
            elif isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False, sort_keys=True)
            lines.append(f"- {key}: {value}")
        lines.append("")
        return "\n".join(lines)

    @staticmethod
    def _write_json(path: Path, document: Mapping[str, Any]) -> None:
        TaskStore._write_text(path, json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n")

    @staticmethod
    def _write_text(path: Path, content: str) -> None:
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
            raise TaskError(f"unable to persist task record: {type(exc).__name__}") from exc


def validate_task_catalog_binding(task: Task, catalog: Mapping[str, Any]) -> None:
    """Bind a task envelope to one exact normalized catalog grant.

    Task records are intentionally durable and leader-owned, but their routing
    fields must not become a second authorization source.  The catalog remains
    authoritative for repository access, worktree ownership, identity
    references, and Discord routing.
    """

    agents = catalog.get("agents") if isinstance(catalog, Mapping) else None
    agent = agents.get(task.agent_id) if isinstance(agents, Mapping) else None
    if not isinstance(agent, Mapping):
        raise TaskError("task agent is not present in the catalog")
    worktree = agent.get("worktree")
    delivery = agent.get("delivery")
    discord = agent.get("discord")
    grants = agent.get("repository_grants")
    if not isinstance(worktree, Mapping) or not isinstance(delivery, Mapping) or not isinstance(discord, Mapping) or not isinstance(grants, list):
        raise TaskError("catalog agent has an incomplete task-routing contract")
    if task.worktree_path != worktree.get("path"):
        raise TaskError("task worktree path does not match the catalog")
    expected_mount_root = worktree.get("container_mount_path")
    expected_mount = f"{expected_mount_root}/{task.checkout_subpath}" if isinstance(expected_mount_root, str) else None
    if task.container_mount_path != expected_mount:
        raise TaskError("task container mount path does not match the catalog checkout")
    if task.gitlab_identity_ref != delivery.get("gitlab_identity_ref"):
        raise TaskError("task GitLab identity reference does not match the catalog")
    if task.delivery_owner != task.agent_id:
        raise TaskError("task delivery owner must be the assigned agent")
    matching = [
        grant
        for grant in grants
        if isinstance(grant, Mapping)
        and grant.get("repository") == task.repository
        and grant.get("checkout_subpath") == task.checkout_subpath
    ]
    if len(matching) != 1:
        raise TaskError("task repository and checkout do not match exactly one catalog grant")
    grant = matching[0]
    if task.base_branch != grant.get("base_branch"):
        raise TaskError("task base branch does not match the catalog grant")
    role = agent.get("role")
    channel_key = "entry_channel_id" if role == "leader" else "work_channel_id"
    if task.reply_to != discord.get(channel_key):
        raise TaskError("task reply channel does not match the catalog agent channel")
    if task.kind == "code":
        if role != "developer":
            raise TaskError("code tasks require a developer agent")
        if grant.get("access") != "write":
            raise TaskError("code tasks require a write repository grant")


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _has_cancellation_evidence(evidence: Mapping[str, Any]) -> bool:
    """Recognize an explicit cancellation request without treating silence as one."""

    return "cancellation_reason" in evidence or "cancellation_decider" in evidence


def _require_timezone(value: str, field: str) -> None:
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise TaskError(f"{field} must be an ISO-8601 timestamp") from exc
    if timestamp.tzinfo is None:
        raise TaskError(f"{field} must include a timezone")


def _safe_markdown(content: str, title: str) -> str:
    if not isinstance(content, str) or not content.strip():
        raise TaskError(f"{title} cannot be empty")
    if CREDENTIAL.search(content):
        raise TaskError(f"{title} contains a credential-shaped value")
    return content.rstrip() + "\n"


def _safe_evidence(value: Mapping[str, Any]) -> dict[str, Any]:
    def safe_value(item: Any) -> Any:
        if isinstance(item, Mapping):
            result: dict[str, Any] = {}
            for key, nested in item.items():
                text_key = str(key)
                result[text_key] = "[REDACTED]" if re.search(r"(?:token|password|secret|private[_-]?key|api[_-]?key)", text_key, re.I) else safe_value(nested)
            return result
        if isinstance(item, list):
            return [safe_value(nested) for nested in item]
        # Reuse CREDENTIAL rather than a looser inline pattern.  A bare `sk-`
        # with no length requirement matches inside ordinary task text --
        # "task-001" contains it -- so every task branch, push ref and leader
        # summary mentioning a task ID was being redacted out of the
        # acceptance record, destroying the evidence the record exists for.
        if isinstance(item, str) and CREDENTIAL.search(item):
            return "[REDACTED]"
        return item

    result: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            continue
        if re.search(r"(?:token|password|secret|private[_-]?key|api[_-]?key)", key, re.I):
            result[key] = "[REDACTED]"
        else:
            result[key] = safe_value(item)
    return result
