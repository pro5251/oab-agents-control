"""Leader runtime capability contract and observations."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import re
from typing import Any


def managed_codex_config() -> str:
    return '\n'.join([
        'sandbox_mode = "danger-full-access"',
        'approval_policy = "never"',
        '',
        '[features]',
        'multi_agent = false',
        '',
    ])


def managed_codex_config_digest() -> str:
    return hashlib.sha256(managed_codex_config().encode()).hexdigest()


def observe_codex_features(output: str, *, image_digest: str, config_digest: str) -> dict[str, Any]:
    """Parse a fresh `codex features list` result into fail-closed evidence."""

    match = re.search(r"(?m)^multi_agent\s+\S+\s+(true|false)\s*$", output) if isinstance(output, str) else None
    conformant = bool(match and match.group(1) == "false" and image_digest and config_digest == managed_codex_config_digest())
    return {
        "leader_subagents": "conformant" if conformant else "non_conformant",
        "multi_agent": False if match and match.group(1) == "false" else (True if match else None),
        "image_digest": image_digest,
        "config_digest": config_digest,
        "observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
