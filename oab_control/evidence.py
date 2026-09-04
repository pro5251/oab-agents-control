"""Facts read from an agent's checkout, rather than claimed by the agent.

The acceptance gate accepts two kinds of evidence and, until now, stored them
identically: ``commit_sha`` and ``developer_tests`` looked the same in
``acceptance.md`` even though one can be checked against the repository and the
other is a report someone typed.  Worse, the operator transcribed both by hand,
so a verifiable fact was only as reliable as the transcription.

This module collects the verifiable half directly.  Nothing here asks the agent
anything: every value comes from git operations against the checkout on the
host, which the agent cannot fake because it does not control the host side of
the mount.

What deliberately stays out: ``push_ref`` and ``merge_request``.  Agents have no
push path by design, so those are the operator's actions and remain attested --
see docs/規格-agent-工作流程.md.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
from typing import Any, Mapping


class EvidenceError(RuntimeError):
    """Raised when a checkout cannot support the claim being made about it."""


#: Acceptance fields this module can establish from the repository itself.
#: Anything not listed here is attested and must be labelled as such.
VERIFIED_FIELDS = ("commit_sha", "delivery_branch", "delivery_repository", "delivery_owner")


@dataclass(frozen=True)
class CollectedEvidence:
    """Verified acceptance fields plus the context that justifies them."""

    fields: dict[str, str]
    context: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {"verified": dict(self.fields), "context": dict(self.context)}


def _git(checkout: Path, *args: str) -> str:
    try:
        process = subprocess.run(
            ["git", "-C", str(checkout), *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
            env={"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        detail = getattr(exc, "stderr", "") or type(exc).__name__
        raise EvidenceError(f"git read failed: {str(detail).strip()[:200]}") from exc
    return process.stdout.strip()


def locate_checkout(task: Any, catalog: Mapping[str, Any]) -> Path:
    """Resolve the one checkout this task is bound to, via the catalog.

    The task record is not an authorization source, so the path is derived from
    the catalog grant rather than from the task's own fields; the task only
    selects which grant applies.
    """

    agents = catalog.get("agents") if isinstance(catalog, Mapping) else None
    agent = agents.get(task.agent_id) if isinstance(agents, Mapping) else None
    if not isinstance(agent, Mapping):
        raise EvidenceError("task agent is not present in the catalog")
    matching = [
        grant
        for grant in agent["repository_grants"]
        if grant["repository"] == task.repository and grant["checkout_subpath"] == task.checkout_subpath
    ]
    if len(matching) != 1:
        raise EvidenceError("task repository and checkout do not match exactly one catalog grant")
    root = Path(agent["worktree"]["path"]).resolve(strict=False)
    checkout = (root / matching[0]["checkout_subpath"]).resolve(strict=False)
    if not checkout.is_relative_to(root) or checkout == root:
        raise EvidenceError("checkout escapes the agent worktree")
    return checkout


def collect(task: Any, catalog: Mapping[str, Any]) -> CollectedEvidence:
    """Read the verifiable acceptance fields, failing closed on any mismatch.

    A refusal here is informative: it means the checkout does not support the
    claim the operator is about to make, which is exactly when a gate should
    stop rather than warn.
    """

    checkout = locate_checkout(task, catalog)
    if not checkout.is_dir() or not (checkout / ".git").exists():
        raise EvidenceError(f"checkout is not materialized: {checkout}")
    if _git(checkout, "rev-parse", "--is-inside-work-tree") != "true":
        raise EvidenceError(f"checkout is not a git work tree: {checkout}")

    branch = _git(checkout, "branch", "--show-current")
    if branch != task.branch:
        raise EvidenceError(f"checkout is on '{branch}' but the task declares '{task.branch}'")

    dirty = _git(checkout, "status", "--porcelain")
    if dirty:
        raise EvidenceError(
            "checkout has uncommitted changes; the delivered content cannot be determined"
        )

    base = task.base_branch
    if not _git(checkout, "rev-parse", "--verify", "--quiet", base):
        raise EvidenceError(f"base branch is not present in the checkout: {base}")

    head = _git(checkout, "rev-parse", "HEAD")
    ahead = _git(checkout, "rev-list", "--count", f"{base}..HEAD")
    if ahead == "0":
        raise EvidenceError(
            f"no commits on {task.branch} beyond {base}; there is nothing to accept"
        )
    changed = [line for line in _git(checkout, "diff", "--name-only", f"{base}...HEAD").splitlines() if line]

    return CollectedEvidence(
        fields={
            "commit_sha": head,
            "delivery_branch": branch,
            "delivery_repository": task.repository,
            "delivery_owner": task.agent_id,
        },
        context={
            "checkout": str(checkout),
            "base_branch": base,
            "commits_ahead": int(ahead),
            "changed_file_count": len(changed),
            "changed_files": changed[:50],
            "worktree_clean": True,
        },
    )


def merge_into(evidence: Mapping[str, Any], collected: CollectedEvidence) -> dict[str, Any]:
    """Fill the verified fields, refusing to paper over a disagreement.

    If the operator supplied one of these and it differs from the repository,
    that is worth stopping for: either the transcription is wrong or the report
    it came from was.  Silently overwriting would hide both.
    """

    merged = dict(evidence)
    for key, value in collected.fields.items():
        supplied = merged.get(key)
        if isinstance(supplied, str) and supplied.strip() and supplied.strip() != value:
            raise EvidenceError(
                f"supplied {key} '{supplied.strip()}' does not match the checkout '{value}'"
            )
        merged[key] = value
    return merged
