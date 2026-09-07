"""Pull Discord assignment receipts into provenance-bound records."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Mapping, Sequence

from .assignments import AssignmentStore
from .discord_dispatch import DispatchError, _validate_binding
from .tasks import CREDENTIAL, TaskStore


class ReceiptError(RuntimeError):
    """Raised when no admissible assignment receipt can be proven."""


def sync_assignment(
    *,
    store: AssignmentStore,
    assignment_id: str,
    catalog: Mapping[str, Any],
    discover_thread: Callable[[str, str], str],
    read_messages: Callable[[str], Sequence[Mapping[str, Any]]],
) -> dict[str, Any]:
    assignment = store.find(assignment_id)
    if assignment.status not in {"dispatched", "acknowledged"}:
        raise ReceiptError("assignment must be dispatched before sync")
    try:
        _validate_binding(store, assignment, catalog)
    except DispatchError as exc:
        raise ReceiptError("assignment binding is no longer valid") from exc
    attempts = [item for item in store.attempts(assignment) if item.get("state") == "delivered" and item.get("message_id")]
    if not attempts:
        raise ReceiptError("assignment has no delivered dispatch provenance")
    dispatch = attempts[-1]
    thread_id = discover_thread(assignment.channel_id, dispatch["message_id"])
    if not isinstance(thread_id, str) or not thread_id:
        raise ReceiptError("dispatch thread is not available")
    target = _target(catalog, assignment.target_agent_id, assignment.target_role)
    discord = target.get("discord")
    expected_author = discord.get("bot_user_id") if isinstance(discord, Mapping) else None
    if expected_author != assignment.target_bot_user_id:
        raise ReceiptError("reviewer catalog binding drifted")
    task = TaskStore(store.tasks_root).get(assignment.task_id)
    for message in read_messages(thread_id):
        if assignment.phase != "review":
            parsed = _admit_execution(
                message,
                assignment_id=assignment.assignment_id,
                thread_id=thread_id,
                author_id=assignment.target_bot_user_id,
                delivered_at=dispatch["resolved_at"],
                completion_marker=task.completion_marker,
            )
            if parsed is None:
                continue
            status = parsed["status"]
            store.set_status(assignment, status, event=f"assignment_{status}", details={"response_message_id": message["id"]})
            return {"assignment_id": assignment.assignment_id, "response_message_id": message["id"], "status": status}
        parsed = _admit(message, assignment_id=assignment.assignment_id, subject=_subject_digest(assignment.subject), thread_id=thread_id, author_id=assignment.target_bot_user_id, delivered_at=dispatch["resolved_at"])
        if parsed is None:
            continue
        record = {
            "task_id": assignment.task_id,
            "assignment_id": assignment.assignment_id,
            "reviewer_agent_id": assignment.target_agent_id,
            "reviewer_role": assignment.target_role,
            "reviewer_bot_user_id": assignment.target_bot_user_id,
            "subject": assignment.subject,
            "channel_id": assignment.channel_id,
            "dispatch_message_id": dispatch["message_id"],
            "thread_id": thread_id,
            "delivered_at": dispatch["resolved_at"],
            "response_message_id": message["id"],
            "response_author_bot_user_id": message["author_id"],
            "received_at": message["timestamp"],
            "verdict": parsed["verdict"],
            "findings": parsed["findings"],
            "status": "completed",
        }
        return store.write_review_record(assignment, record)
    raise ReceiptError("no matching target response was found in the assignment thread")


def _admit_execution(
    message: Mapping[str, Any],
    *,
    assignment_id: str,
    thread_id: str,
    author_id: str,
    delivered_at: str,
    completion_marker: str,
) -> dict[str, str] | None:
    """Admit an acknowledgement or completion without inventing a review."""

    if message.get("channel_id") != thread_id or message.get("author_id") != author_id:
        return None
    identifier = message.get("id")
    timestamp = message.get("timestamp")
    content = message.get("content")
    if not all(isinstance(value, str) and value for value in (identifier, timestamp, content)):
        return None
    try:
        if datetime.fromisoformat(timestamp.replace("Z", "+00:00")) <= datetime.fromisoformat(delivered_at.replace("Z", "+00:00")):
            return None
    except ValueError:
        return None
    fields: dict[str, str] = {}
    for line in content.splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            fields[key.strip().lower()] = value.strip()
    if fields.get("assignment") != assignment_id:
        return None
    if fields.get("status") not in {None, "acknowledged", "completed"}:
        return None
    if CREDENTIAL.search(content):
        return {"status": "acknowledged"}
    marker = fields.get("completion") or fields.get("completion_marker") or ""
    complete = fields.get("status") == "completed" and (not completion_marker or marker == completion_marker)
    if not complete and completion_marker and completion_marker in content:
        complete = True
    return {"status": "completed" if complete else "acknowledged"}


def _admit(message: Mapping[str, Any], *, assignment_id: str, subject: str, thread_id: str, author_id: str, delivered_at: str) -> dict[str, str] | None:
    if message.get("channel_id") != thread_id or message.get("author_id") != author_id:
        return None
    identifier = message.get("id")
    timestamp = message.get("timestamp")
    content = message.get("content")
    if not all(isinstance(value, str) and value for value in (identifier, timestamp, content)):
        return None
    try:
        if datetime.fromisoformat(timestamp.replace("Z", "+00:00")) <= datetime.fromisoformat(delivered_at.replace("Z", "+00:00")):
            return None
    except ValueError:
        return None
    fields: dict[str, str] = {}
    for line in content.splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            fields[key.strip().lower()] = value.strip()
    if fields.get("assignment") != assignment_id or fields.get("subject") != subject:
        return None
    verdict = fields.get("verdict")
    findings = fields.get("findings")
    if verdict not in {"passed", "changes_requested", "inconclusive"} or not findings:
        return None
    if CREDENTIAL.search(findings):
        findings = "[REDACTED credential-shaped content]"
    return {"verdict": verdict, "findings": findings}


def _subject_digest(subject: Mapping[str, Any]) -> str:
    value = subject.get("commit_sha") or subject.get("sha256")
    if not isinstance(value, str) or not value:
        raise ReceiptError("review assignment has no immutable subject digest")
    return value


def _target(catalog: Mapping[str, Any], agent_id: str, expected_role: str) -> Mapping[str, Any]:
    agents = catalog.get("agents") if isinstance(catalog, Mapping) else None
    target = agents.get(agent_id) if isinstance(agents, Mapping) else None
    if not isinstance(target, Mapping) or target.get("role") != expected_role:
        raise ReceiptError("assignment target role binding drifted")
    return target
