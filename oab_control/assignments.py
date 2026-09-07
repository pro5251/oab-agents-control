"""Control-plane Work Assignment records and lifecycle invariants."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Any, Callable, Iterator, Mapping
import uuid

from .tasks import CREDENTIAL, TASK_ID, Task, TaskError, TaskStore, validate_task_catalog_binding


class AssignmentError(ValueError):
    """Raised when an assignment cannot be created or changed safely."""


ASSIGNMENT_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
PHASES = {"execution", "review"}
STATUSES = {
    "prepared",
    "dispatched",
    "acknowledged",
    "completed",
    "superseded",
    "cancelled",
    "needs_reconciliation",
}
STATUS_TRANSITIONS = {
    "prepared": {"dispatched", "needs_reconciliation", "superseded", "cancelled"},
    "dispatched": {"acknowledged", "completed", "needs_reconciliation", "superseded", "cancelled"},
    "acknowledged": {"completed", "needs_reconciliation", "superseded", "cancelled"},
    "needs_reconciliation": {"prepared", "dispatched", "superseded", "cancelled"},
    "completed": {"superseded"},
    "superseded": set(),
    "cancelled": set(),
}


@dataclass(frozen=True)
class WorkAssignment:
    assignment_id: str
    task_id: str
    phase: str
    sequence: int
    target_agent_id: str
    target_role: str
    repository: str
    checkout_subpath: str
    host_checkout_path: str
    container_checkout_path: str
    channel_id: str
    target_bot_user_id: str
    subject: dict[str, Any]
    inbox_path: str
    status: str
    created_at: str
    updated_at: str

    def __post_init__(self) -> None:
        if not ASSIGNMENT_ID.fullmatch(self.assignment_id):
            raise AssignmentError("unsafe assignment ID")
        if self.phase not in PHASES or self.status not in STATUSES:
            raise AssignmentError("invalid assignment phase or status")
        if not isinstance(self.sequence, int) or isinstance(self.sequence, bool) or self.sequence < 1:
            raise AssignmentError("assignment sequence must be positive")
        required = (
            self.task_id,
            self.target_agent_id,
            self.target_role,
            self.repository,
            self.checkout_subpath,
            self.host_checkout_path,
            self.container_checkout_path,
            self.channel_id,
            self.target_bot_user_id,
            self.inbox_path,
            self.created_at,
            self.updated_at,
        )
        if any(not isinstance(value, str) or not value for value in required):
            raise AssignmentError("assignment binding fields cannot be empty")
        if not TASK_ID.fullmatch(self.task_id):
            raise AssignmentError("unsafe task ID")
        if not isinstance(self.subject, dict) or not self.subject.get("kind"):
            raise AssignmentError("assignment subject is required")

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class AssignmentStore:
    """Persist assignment projections and events beneath Delivery Tasks."""

    def __init__(self, tasks_root: str | Path):
        self.tasks_root = Path(tasks_root)

    def prepare(
        self,
        *,
        task_id: str,
        phase: str,
        target_agent_id: str,
        catalog: Mapping[str, Any],
        inbox_root: str | Path,
        subject: Mapping[str, Any] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> WorkAssignment:
        if phase not in PHASES:
            raise AssignmentError("assignment phase must be execution or review")
        task = TaskStore(self.tasks_root).get(task_id)
        required_state = "assigned" if phase == "execution" else "review"
        if task.state != required_state:
            raise AssignmentError(f"{phase} assignment requires a {required_state} Delivery Task")
        try:
            validate_task_catalog_binding(task, catalog)
        except TaskError as exc:
            raise AssignmentError(str(exc)) from exc
        agent = _catalog_agent(catalog, target_agent_id)
        role = agent.get("role")
        expected_roles = {"reviewer"} if phase == "review" else ({"developer"} if task.kind == "code" else {"researcher", "developer"})
        if role not in expected_roles:
            expected = "/".join(sorted(expected_roles))
            raise AssignmentError(f"{task.kind} {phase} assignment requires a {expected} agent")
        if phase == "execution" and target_agent_id != task.agent_id:
            raise AssignmentError("execution assignment target must match the Delivery Task owner")
        grant = _matching_grant(agent, task)
        if phase == "execution" and task.kind == "code" and grant.get("access") != "write":
            raise AssignmentError("code execution assignment requires a developer write grant")
        if phase == "review" and grant.get("access") != "read":
            raise AssignmentError("review assignment requires a reviewer read grant")
        fixed_subject = dict(subject or ({"kind": "task", "task_id": task_id} if phase == "execution" else {}))
        if phase == "review":
            _validate_review_subject(task, fixed_subject)
        worktree = agent.get("worktree")
        discord = agent.get("discord")
        if not isinstance(worktree, Mapping) or not isinstance(discord, Mapping):
            raise AssignmentError("target catalog binding is incomplete")
        host_checkout = Path(str(worktree.get("path"))) / task.checkout_subpath
        if not host_checkout.is_dir():
            raise AssignmentError("target checkout is not materialized")
        agent_inbox = Path(inbox_root) / target_agent_id
        if not agent_inbox.is_dir():
            raise AssignmentError("target agent inbox root does not exist")

        with self._lock(task_id):
            existing = self.list(task_id)
            if any(item.phase == "execution" and item.status not in {"completed", "superseded", "cancelled"} for item in existing):
                raise AssignmentError("task already has an unfinished execution assignment")
            if phase == "review" and any(item.phase == "review" and item.status not in {"completed", "superseded", "cancelled"} for item in existing):
                raise AssignmentError("task already has an unfinished review assignment")
            if any(
                item.target_agent_id == target_agent_id
                and item.status not in {"completed", "superseded", "cancelled"}
                for other_task in TaskStore(self.tasks_root).list()
                if other_task.task_id != task_id
                for item in self.list(other_task.task_id)
            ):
                raise AssignmentError("target agent already has an unfinished assignment")
            assignment_id = (id_factory or (lambda: f"assignment-{uuid.uuid4().hex[:12]}"))()
            if not isinstance(assignment_id, str) or not ASSIGNMENT_ID.fullmatch(assignment_id):
                raise AssignmentError("unsafe assignment ID")
            if any(item.assignment_id == assignment_id for item in existing):
                raise AssignmentError("assignment already exists")
            sequence = max((item.sequence for item in existing), default=0) + 1
            now = _now()
            assignment_dir = self._assignment_dir(task_id, assignment_id)
            inbox_dir = agent_inbox / assignment_id
            assignment = WorkAssignment(
                assignment_id=assignment_id,
                task_id=task_id,
                phase=phase,
                sequence=sequence,
                target_agent_id=target_agent_id,
                target_role=str(role),
                repository=task.repository,
                checkout_subpath=task.checkout_subpath,
                host_checkout_path=str(host_checkout),
                container_checkout_path=f"{worktree['container_mount_path']}/{task.checkout_subpath}",
                channel_id=_required_text(discord, "work_channel_id"),
                target_bot_user_id=_required_text(discord, "bot_user_id"),
                subject=fixed_subject,
                inbox_path=f"/oab/tasks/{assignment_id}/TASK.md",
                status="prepared",
                created_at=now,
                updated_at=now,
            )
            assignment_root = self.tasks_root / task_id / "assignments"
            assignment_root.mkdir(parents=True, mode=0o700)
            os.chmod(assignment_root, 0o700)
            assignment_dir.mkdir(parents=True, mode=0o700)
            os.chmod(assignment_dir, 0o700)
            inbox_dir.mkdir(mode=0o700)
            os.chmod(inbox_dir, 0o700)
            _atomic_json(assignment_dir / "assignment.json", assignment.as_dict())
            _append_jsonl(assignment_dir / "events.jsonl", {
                "at": now,
                "event": "prepared",
                "assignment_id": assignment_id,
                "task_id": task_id,
                "status": "prepared",
            })
            _atomic_text(inbox_dir / "TASK.md", _brief(task, assignment))
            return assignment

    def get(self, task_id: str, assignment_id: str) -> WorkAssignment:
        if not ASSIGNMENT_ID.fullmatch(assignment_id):
            raise AssignmentError("unsafe assignment ID")
        try:
            document = json.loads((self._assignment_dir(task_id, assignment_id) / "assignment.json").read_text(encoding="utf-8"))
            return WorkAssignment(**document)
        except FileNotFoundError as exc:
            raise AssignmentError(f"unknown assignment: {assignment_id}") from exc
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            raise AssignmentError(f"invalid assignment record: {assignment_id}") from exc

    def list(self, task_id: str) -> list[WorkAssignment]:
        root = self.tasks_root / task_id / "assignments"
        if not root.exists():
            return []
        result: list[WorkAssignment] = []
        for path in sorted(root.iterdir()):
            if path.is_dir() and ASSIGNMENT_ID.fullmatch(path.name) and (path / "assignment.json").is_file():
                result.append(self.get(task_id, path.name))
        return result

    def find(self, assignment_id: str) -> WorkAssignment:
        if not ASSIGNMENT_ID.fullmatch(assignment_id):
            raise AssignmentError("unsafe assignment ID")
        matches: list[WorkAssignment] = []
        for task in TaskStore(self.tasks_root).list():
            try:
                matches.append(self.get(task.task_id, assignment_id))
            except AssignmentError as exc:
                if not str(exc).startswith("unknown assignment"):
                    raise
        if len(matches) != 1:
            raise AssignmentError(f"unknown or ambiguous assignment: {assignment_id}")
        return matches[0]

    def set_status(self, assignment: WorkAssignment, status: str, *, event: str, details: Mapping[str, Any] | None = None) -> WorkAssignment:
        if status not in STATUSES:
            raise AssignmentError("invalid assignment status")
        with self._lock(assignment.task_id):
            current = self.get(assignment.task_id, assignment.assignment_id)
            if status != current.status and status not in STATUS_TRANSITIONS[current.status]:
                raise AssignmentError(f"invalid assignment transition: {current.status} -> {status}")
            updated = WorkAssignment(**{**current.as_dict(), "status": status, "updated_at": _now()})
            directory = self._assignment_dir(current.task_id, current.assignment_id)
            _atomic_json(directory / "assignment.json", updated.as_dict())
            _append_jsonl(directory / "events.jsonl", {
                "at": updated.updated_at,
                "event": event,
                "assignment_id": updated.assignment_id,
                "task_id": updated.task_id,
                "status": status,
                "details": dict(details or {}),
            })
            return updated

    def supersede(self, assignment: WorkAssignment, *, reason: str) -> WorkAssignment:
        """Invalidate a review assignment and record the projection change."""

        if assignment.phase != "review":
            raise AssignmentError("only review assignments can be superseded")
        return self.set_status(assignment, "superseded", event="review_superseded", details={"reason": reason})

    def append_attempt(self, assignment: WorkAssignment, attempt: Mapping[str, Any]) -> None:
        with self._lock(assignment.task_id):
            current = self.get(assignment.task_id, assignment.assignment_id)
            if current.status not in {"prepared", "needs_reconciliation"}:
                raise AssignmentError("assignment is not dispatchable")
            _append_jsonl(self._assignment_dir(current.task_id, current.assignment_id) / "dispatch-attempts.jsonl", attempt)

    def attempts(self, assignment: WorkAssignment) -> list[dict[str, Any]]:
        """Return the current projection of append-only Dispatch Attempts.

        A dispatch retry appends a new state for the same attempt ID.  Callers
        must not treat the historical pending/unknown rows as still live.
        ``attempt_history`` remains available when an audit needs every row.
        """
        history = self.attempt_history(assignment)
        projected: dict[str, dict[str, Any]] = {}
        for item in history:
            attempt_id = item.get("attempt_id")
            if not isinstance(attempt_id, str) or not attempt_id:
                raise AssignmentError("dispatch attempt is missing attempt_id")
            projected[attempt_id] = item
        return list(projected.values())

    def attempt_history(self, assignment: WorkAssignment) -> list[dict[str, Any]]:
        path = self._assignment_dir(assignment.task_id, assignment.assignment_id) / "dispatch-attempts.jsonl"
        if not path.exists():
            return []
        try:
            return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
        except (OSError, json.JSONDecodeError) as exc:
            raise AssignmentError("invalid dispatch attempt log") from exc

    def write_review_record(self, assignment: WorkAssignment, record: Mapping[str, Any]) -> dict[str, Any]:
        if assignment.phase != "review":
            raise AssignmentError("Review Record requires a review assignment")
        with self._lock(assignment.task_id):
            current = self.get(assignment.task_id, assignment.assignment_id)
            if current.status not in {"dispatched", "acknowledged"}:
                raise AssignmentError("review assignment is not awaiting a result")
            document = dict(record)
            if document.get("assignment_id") != current.assignment_id or document.get("task_id") != current.task_id or document.get("subject") != current.subject:
                raise AssignmentError("Review Record binding does not match the assignment")
            path = self._assignment_dir(current.task_id, current.assignment_id) / "review.json"
            if path.exists():
                raise AssignmentError("Review Record already exists")
            _atomic_json(path, document)
        self.set_status(current, "completed", event="review_completed", details={"response_message_id": document.get("response_message_id", "")})
        return document

    def review_record(self, assignment: WorkAssignment) -> dict[str, Any] | None:
        path = self._assignment_dir(assignment.task_id, assignment.assignment_id) / "review.json"
        if not path.exists():
            return None
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AssignmentError("invalid Review Record") from exc
        if not isinstance(document, dict):
            raise AssignmentError("invalid Review Record")
        return document

    def prune(
        self,
        assignment: WorkAssignment,
        *,
        inbox_root: str | Path,
        mirror: str | Path | None = None,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        task = TaskStore(self.tasks_root).get(assignment.task_id)
        if task.state != "closed":
            raise AssignmentError("assignment prune requires a closed Delivery Task")
        current = self.get(assignment.task_id, assignment.assignment_id)
        if current.status not in {"prepared", "completed", "superseded", "cancelled"}:
            raise AssignmentError("assignment prune requires a terminal assignment")
        attempts = self.attempts(assignment)
        if any(item.get("state") in {"pending", "unknown"} for item in attempts):
            raise AssignmentError("assignment prune requires reconciled dispatch attempts")
        if assignment.phase == "review" and self.review_record(assignment) is None:
            raise AssignmentError("assignment prune requires the permanent Review Record")
        subject = assignment.subject
        inbox_root_path = Path(inbox_root).resolve(strict=True)
        inbox_parent = (inbox_root_path / assignment.target_agent_id).resolve(strict=False)
        if not inbox_parent.is_relative_to(inbox_root_path):
            raise AssignmentError("assignment inbox escapes the configured root")
        inbox_target = inbox_parent / assignment.assignment_id
        if inbox_target.exists() and (inbox_target.is_symlink() or not inbox_target.resolve().is_relative_to(inbox_parent)):
            raise AssignmentError("assignment inbox is not contained by the configured root")
        targets = {
            "inbox": str(inbox_target),
            "snapshot": subject.get("snapshot_path", ""),
            "review_ref": subject.get("review_ref", ""),
        }
        if not confirmed:
            return {"confirmed": False, "pruned": False, "targets": targets}
        inbox = Path(targets["inbox"])
        if inbox.exists():
            shutil.rmtree(inbox)
        snapshot = Path(targets["snapshot"]) if targets["snapshot"] else None
        if snapshot is not None:
            task_root = (self.tasks_root / assignment.task_id).resolve()
            if not snapshot.resolve().is_relative_to(task_root / "artifacts"):
                raise AssignmentError("artifact snapshot escapes the task artifact root")
            if snapshot.exists():
                snapshot.unlink()
        review_ref = targets["review_ref"]
        if review_ref and mirror is None:
            raise AssignmentError("code review prune requires the delivery mirror")
        if mirror is not None and review_ref:
            mirror_path = Path(mirror).resolve(strict=True)
            expected = subject.get("commit_sha")
            current = _git_optional(mirror_path, "rev-parse", "--verify", review_ref)
            if current and current != expected:
                raise AssignmentError("review ref changed before prune")
            if current:
                subprocess.run(["git", "update-ref", "-d", review_ref, expected], cwd=mirror_path, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        _append_jsonl(self._assignment_dir(assignment.task_id, assignment.assignment_id) / "events.jsonl", {
            "at": _now(), "event": "pruned", "assignment_id": assignment.assignment_id,
            "task_id": assignment.task_id, "status": assignment.status, "targets": targets,
        })
        return {"confirmed": True, "pruned": True, "targets": targets}

    def _assignment_dir(self, task_id: str, assignment_id: str) -> Path:
        return self.tasks_root / task_id / "assignments" / assignment_id

    @contextmanager
    def _lock(self, task_id: str) -> Iterator[None]:
        task_dir = self.tasks_root / task_id
        if not task_dir.is_dir():
            raise AssignmentError(f"unknown task: {task_id}")
        self.tasks_root.mkdir(parents=True, exist_ok=True)
        global_path = self.tasks_root / ".assignment-global.lock"
        global_descriptor = os.open(global_path, os.O_CREAT | os.O_RDWR, 0o600)
        os.fchmod(global_descriptor, 0o600)
        fcntl.flock(global_descriptor, fcntl.LOCK_EX)
        path = task_dir / ".assignment.lock"
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.chmod(path, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
            fcntl.flock(global_descriptor, fcntl.LOCK_UN)
            os.close(global_descriptor)


def _catalog_agent(catalog: Mapping[str, Any], agent_id: str) -> Mapping[str, Any]:
    agents = catalog.get("agents") if isinstance(catalog, Mapping) else None
    agent = agents.get(agent_id) if isinstance(agents, Mapping) else None
    if not isinstance(agent, Mapping) or agent.get("role") == "leader":
        raise AssignmentError("assignment target must be a catalog worker")
    return agent


def _matching_grant(agent: Mapping[str, Any], task: Task) -> Mapping[str, Any]:
    grants = agent.get("repository_grants")
    if not isinstance(grants, list):
        raise AssignmentError("target agent has no repository grants")
    matches = [item for item in grants if isinstance(item, Mapping) and item.get("repository") == task.repository and item.get("checkout_subpath") == task.checkout_subpath]
    if len(matches) != 1:
        raise AssignmentError("target grant does not match the Delivery Task repository")
    return matches[0]


def _required_text(value: Mapping[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise AssignmentError(f"target catalog field is missing: {key}")
    return item


def _validate_review_subject(task: Task, subject: Mapping[str, Any]) -> None:
    if task.kind == "code":
        required = {"kind", "repository", "base_ref", "base_sha", "commit_sha", "review_ref"}
        if set(subject) != required or subject.get("kind") != "git_commit" or subject.get("repository") != task.repository:
            raise AssignmentError("code review subject does not match the Delivery Task repository")
        base_sha = subject.get("base_sha")
        commit_sha = subject.get("commit_sha")
        if not all(isinstance(value, str) and re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value) for value in (base_sha, commit_sha)):
            raise AssignmentError("code review subject requires exact Git SHAs")
        expected_ref = f"refs/oab/reviews/{task.task_id}/{commit_sha}"
        if subject.get("review_ref") != expected_ref or subject.get("base_ref") != task.base_branch:
            raise AssignmentError("code review subject ref or base does not match the task")
    else:
        if set(subject) != {"kind", "artifact_name", "sha256", "snapshot_path"} or subject.get("kind") != "artifact":
            raise AssignmentError("non-code review subject must be an artifact snapshot")
        if not isinstance(subject.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", subject["sha256"]):
            raise AssignmentError("artifact review subject requires a SHA-256 digest")


def _brief(task: Task, assignment: WorkAssignment) -> str:
    subject = assignment.subject.get("commit_sha") or assignment.subject.get("sha256") or assignment.task_id
    return "\n".join([
        "# Work Assignment",
        "",
        f"- task: `{task.task_id}`",
        f"- assignment: `{assignment.assignment_id}`",
        f"- role: `{assignment.target_role}`",
        f"- phase: `{assignment.phase}`",
        f"- subject: `{subject}`",
        f"- checkout: `{assignment.container_checkout_path}`",
        f"- reply channel: `{assignment.channel_id}`",
        f"- completion marker: {task.completion_marker}",
        f"- checkpoint: {task.checkpoint}",
        f"- deadline: `{task.deadline}`",
        "",
        "## Goal",
        "",
        task.goal,
        "",
        "## Scope",
        "",
        task.scope,
        "",
        "Reply in the Discord thread created for this assignment and include the assignment ID.",
        "",
        "## Response format",
        "",
        "For a review assignment, reply with exactly these fields (one per line):",
        "`assignment: <assignment id>`",
        f"`subject: <{subject}>`",
        "`verdict: passed|changes_requested|inconclusive`",
        "`findings: <concise findings; state no blocking findings explicitly when applicable>`",
        "For an execution assignment, acknowledge the assignment first and include the completion marker above when the work is complete.",
        "",
    ])


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _atomic_json(path: Path, document: Mapping[str, Any]) -> None:
    _assert_safe_document(document)
    _atomic_text(path, json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _append_jsonl(path: Path, document: Mapping[str, Any]) -> None:
    _assert_safe_document(document)
    payload = (json.dumps(document, ensure_ascii=False, sort_keys=True) + "\n").encode()
    descriptor = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _assert_safe_document(value: Any) -> None:
    """Reject credential-shaped assignment state before it reaches disk."""

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            for key, nested in item.items():
                key_text = str(key)
                if re.search(r"(?:token|password|secret|private[_-]?key|api[_-]?key)", key_text, re.I) and not key_text.endswith("_ref"):
                    raise AssignmentError("assignment record contains a credential-shaped field")
                visit(nested)
        elif isinstance(item, (list, tuple)):
            for nested in item:
                visit(nested)
        elif isinstance(item, str) and CREDENTIAL.search(item):
            raise AssignmentError("assignment record contains a credential-shaped value")

    visit(value)


def _git_optional(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return result.stdout.strip() if result.returncode == 0 else ""
