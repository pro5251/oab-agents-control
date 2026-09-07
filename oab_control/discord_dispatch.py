"""Safe Discord assignment preview and write-ahead dispatch boundary."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from typing import Any, Callable, Mapping
import uuid

from .assignments import AssignmentError, AssignmentStore, WorkAssignment, _matching_grant, _validate_review_subject
from .tasks import TaskError, TaskStore, validate_task_catalog_binding


class DispatchError(RuntimeError):
    """Raised when dispatch is refused or has a non-success outcome."""


class DiscordHttpError(RuntimeError):
    def __init__(self, status: int, *, retry_after: str = ""):
        super().__init__(f"Discord HTTP {status}")
        self.status = status
        self.retry_after = retry_after


def dispatch_assignment(
    *,
    store: AssignmentStore,
    assignment_id: str,
    catalog: Mapping[str, Any],
    confirmed: bool,
    send: Callable[[str, str], str],
    nonce_factory: Callable[[], str] | None = None,
) -> dict[str, Any]:
    assignment = store.find(assignment_id)
    _validate_binding(store, assignment, catalog)
    if assignment.status == "needs_reconciliation" or any(item.get("state") in {"pending", "unknown"} for item in store.attempts(assignment)):
        raise DispatchError("dispatch requires reconciliation before resend")
    if assignment.status != "prepared":
        raise DispatchError("assignment is not prepared for dispatch")
    nonce = (nonce_factory or (lambda: uuid.uuid4().hex))()
    if not isinstance(nonce, str) or not nonce or any(character.isspace() for character in nonce):
        raise DispatchError("dispatch nonce is invalid")
    message = _message(assignment, nonce)
    leader_id, leader_bot = _leader(catalog)
    preview = {
        "confirmed": confirmed,
        "assignment_id": assignment.assignment_id,
        "task_id": assignment.task_id,
        "target_agent_id": assignment.target_agent_id,
        "target_bot_user_id": assignment.target_bot_user_id,
        "channel_id": assignment.channel_id,
        "subject": assignment.subject,
        "inbox_path": assignment.inbox_path,
        "message": message,
    }
    if not confirmed:
        return preview

    attempt_id = f"attempt-{uuid.uuid4().hex[:16]}"
    started = _now()
    base = {
        "attempt_id": attempt_id,
        "nonce": nonce,
        "assignment_id": assignment.assignment_id,
        "channel_id": assignment.channel_id,
        "target_bot_user_id": assignment.target_bot_user_id,
        "author_agent_id": leader_id,
        "author_bot_user_id": leader_bot,
        "content_sha256": hashlib.sha256(message.encode()).hexdigest(),
        "state": "pending",
        "message_id": "",
        "thread_id": "",
        "http_status": None,
        "retry_after": "",
        "started_at": started,
        "resolved_at": "",
    }
    store.append_attempt(assignment, base)
    try:
        message_id = send(assignment.channel_id, message)
        if not isinstance(message_id, str) or not message_id:
            raise TimeoutError("incomplete provider response")
    except DiscordHttpError as exc:
        resolved = {**base, "state": "definitive_failure", "http_status": exc.status, "retry_after": exc.retry_after, "resolved_at": _now()}
        store.append_attempt(assignment, resolved)
        raise DispatchError(f"Discord dispatch failed with HTTP {exc.status}") from exc
    except (TimeoutError, ConnectionError, OSError) as exc:
        resolved = {**base, "state": "unknown", "resolved_at": _now()}
        store.append_attempt(assignment, resolved)
        store.set_status(assignment, "needs_reconciliation", event="dispatch_unknown", details={"attempt_id": attempt_id})
        raise DispatchError("Discord dispatch outcome is unknown; reconciliation required") from exc
    resolved = {**base, "state": "delivered", "message_id": message_id, "http_status": 200, "resolved_at": _now()}
    store.append_attempt(assignment, resolved)
    store.set_status(assignment, "dispatched", event="dispatched", details={"attempt_id": attempt_id, "message_id": message_id})
    return {**preview, **resolved}


def reconcile_assignment(
    *,
    store: AssignmentStore,
    assignment_id: str,
    catalog: Mapping[str, Any],
    find_messages: Callable[[str, str], list[Mapping[str, Any]]],
) -> dict[str, Any]:
    assignment = store.find(assignment_id)
    _validate_binding(store, assignment, catalog)
    if assignment.status != "needs_reconciliation":
        raise DispatchError("assignment does not require reconciliation")
    unknown = [item for item in store.attempts(assignment) if item.get("state") == "unknown"]
    if not unknown:
        raise DispatchError("assignment has no unknown Dispatch Attempt")
    attempt = unknown[-1]
    _leader_id, leader_bot = _leader(catalog)
    matches = [item for item in find_messages(assignment.channel_id, attempt["nonce"]) if item.get("author_id") == leader_bot and isinstance(item.get("id"), str) and item["id"]]
    if len(matches) > 1:
        raise DispatchError("multiple Discord messages matched the dispatch nonce; manual reconciliation required")
    if not matches:
        resolved = {**attempt, "state": "reconciled_not_found", "resolved_at": _now()}
        store.append_attempt(assignment, resolved)
        store.set_status(assignment, "prepared", event="dispatch_reconciled_not_found", details={"attempt_id": attempt["attempt_id"]})
        return resolved
    match = matches[0]
    resolved = {**attempt, "state": "delivered", "message_id": match["id"], "thread_id": str(match.get("thread_id") or ""), "resolved_at": _now()}
    store.append_attempt(assignment, resolved)
    store.set_status(assignment, "dispatched", event="dispatch_reconciled", details={"attempt_id": attempt["attempt_id"], "message_id": match["id"]})
    return resolved


def _message(assignment: WorkAssignment, nonce: str) -> str:
    subject = assignment.subject.get("commit_sha") or assignment.subject.get("sha256") or assignment.task_id
    checkout = assignment.container_checkout_path if assignment.repository else "n/a"
    return "\n".join([
        f"<@{assignment.target_bot_user_id}>",
        f"assignment: {assignment.assignment_id}",
        f"task: {assignment.task_id}",
        f"phase: {assignment.phase}",
        f"subject: {subject}",
        f"checkout: {checkout}",
        f"instructions: {assignment.inbox_path}",
        f"dispatch_nonce: {nonce}",
    ])


def _validate_binding(store: AssignmentStore, assignment: WorkAssignment, catalog: Mapping[str, Any]) -> None:
    try:
        task = TaskStore(store.tasks_root).get(assignment.task_id)
        validate_task_catalog_binding(task, catalog)
    except (TaskError, OSError) as exc:
        raise DispatchError("assignment Delivery Task binding drifted") from exc
    expected_state = "assigned" if assignment.phase == "execution" else "review"
    if task.state != expected_state:
        raise DispatchError(f"{assignment.phase} dispatch requires a {expected_state} Delivery Task")
    expected_role = "reviewer" if assignment.phase == "review" else ("developer" if task.kind == "code" else assignment.target_role)
    if assignment.target_agent_id != (task.agent_id if assignment.phase == "execution" else assignment.target_agent_id):
        raise DispatchError("assignment target no longer matches the Delivery Task owner")
    agents = catalog.get("agents") if isinstance(catalog, Mapping) else None
    target = agents.get(assignment.target_agent_id) if isinstance(agents, Mapping) else None
    if not isinstance(target, Mapping) or target.get("role") != assignment.target_role or target.get("role") != expected_role:
        raise DispatchError("assignment target catalog binding drifted")
    discord = target.get("discord")
    if not isinstance(discord, Mapping) or discord.get("bot_user_id") != assignment.target_bot_user_id or discord.get("work_channel_id") != assignment.channel_id:
        raise DispatchError("assignment Discord binding drifted")
    try:
        grant = _matching_grant(target, task)
    except AssignmentError as exc:
        raise DispatchError("assignment repository grant drifted") from exc
    expected_access = "read" if assignment.phase == "review" or task.kind != "code" else "write"
    if grant.get("access") != expected_access:
        raise DispatchError("assignment repository access grant drifted")
    if assignment.phase == "review":
        try:
            _validate_review_subject(task, assignment.subject)
        except AssignmentError as exc:
            raise DispatchError("assignment review subject drifted") from exc


def _leader(catalog: Mapping[str, Any]) -> tuple[str, str]:
    agents = catalog.get("agents") if isinstance(catalog, Mapping) else None
    leaders = [(key, value) for key, value in agents.items() if isinstance(value, Mapping) and value.get("role") == "leader"] if isinstance(agents, Mapping) else []
    if len(leaders) != 1:
        raise DispatchError("catalog must contain exactly one leader")
    leader_id, leader = leaders[0]
    discord = leader.get("discord")
    bot = discord.get("bot_user_id") if isinstance(discord, Mapping) else None
    if not isinstance(bot, str) or not bot:
        raise DispatchError("leader bot binding is missing")
    return str(leader_id), bot


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
