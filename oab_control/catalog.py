"""Strict, secret-safe validation for the declarative agent catalog.

The validator deliberately has no Kubernetes, Discord, or GitLab client.  It
only checks the contract that those adapters consume, which makes it safe to
run before a cluster or external credential exists.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any, Iterable, Mapping

import yaml

from .yaml_utils import load_yaml


CATALOG_VERSION = 1
DEFAULT_BASE_BRANCH = "origin/develop"
ROLES = {"leader", "researcher", "developer", "reviewer"}
WORKER_ROLES = ROLES - {"leader"}
ACCESS = {"read", "write"}
EGRESS_NAME = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
DISCORD_ID = re.compile(r"^[0-9]{17,20}$")
SECRET_REF = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,61}[A-Za-z0-9])?/[A-Za-z0-9](?:[A-Za-z0-9._-]{0,61}[A-Za-z0-9])?$")
IDENTIFIER = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
QUANTITY = re.compile(r"^(?:[0-9]+(?:\.[0-9]+)?|[0-9]+(?:\.[0-9]+)?(?:m|Ki|Mi|Gi|Ti|Pi|Ei))$")
ABSOLUTE_ENV_OR_GLOB = re.compile(r"(?:\$\{?|~|[*?\[\]])")
SECRET_FIELD = re.compile(r"(?:token|password|secret(?:_value)?|private[_-]?key|api[_-]?key)", re.I)
SECRET_VALUE = re.compile(
    r"(?:^|\b)(?:sk-[A-Za-z0-9_-]{16,}|glpat-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{16,}|xox[baprs]-[A-Za-z0-9-]{16,}|(?:https?|ssh)://[^/\s:@]+:[^@\s]+@|Bot\s+[A-Za-z0-9._-]{16,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|(?:token|password|secret)\s*[:=])",
    re.I,
)


class CatalogError(ValueError):
    """Raised when a YAML document cannot be loaded as a catalog."""


@dataclass(frozen=True)
class Diagnostic:
    path: str
    code: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"path": self.path, "code": self.code, "message": self.message}


class _Validator:
    def __init__(self, *, check_paths: bool, check_git: bool, available_secret_refs: set[str] | None, available_identity_refs: set[str] | None):
        self.errors: list[Diagnostic] = []
        self.check_paths = check_paths
        self.check_git = check_git
        self.available_secret_refs = available_secret_refs
        self.available_identity_refs = available_identity_refs
        self._worktree_paths: list[tuple[str, Path]] = []
        self._mount_paths: list[tuple[str, Path]] = []
        self._channels: dict[str, str] = {}
        self._bots: dict[str, str] = {}
        self._secret_refs: dict[str, str] = {}
        self._repos: dict[tuple[str, str, str], str] = {}

    def error(self, path: str, code: str, message: str) -> None:
        self.errors.append(Diagnostic(path, code, message))

    def mapping(self, value: Any, path: str) -> Mapping[str, Any] | None:
        if not isinstance(value, Mapping):
            self.error(path, "type", "expected a mapping")
            return None
        if not all(isinstance(key, str) for key in value):
            self.error(path, "key_type", "mapping keys must be strings")
            return None
        return value

    def keys(self, value: Mapping[str, Any], allowed: set[str], path: str) -> None:
        for key in sorted(set(value) - allowed):
            self.error(f"{path}.{key}", "unknown_field", "field is not part of catalog version 1")

    def string(self, value: Any, path: str, *, nonempty: bool = True) -> str | None:
        if not isinstance(value, str):
            self.error(path, "type", "expected a string")
            return None
        if nonempty and not value.strip():
            self.error(path, "empty", "must not be empty")
            return None
        self.secret_scan(value, path)
        return value

    def secret_scan(self, value: str, path: str) -> None:
        if SECRET_VALUE.search(value):
            self.error(path, "secret_value", "credential values are not allowed; use a reference")

    def list_of_strings(self, value: Any, path: str) -> list[str] | None:
        if not isinstance(value, list):
            self.error(path, "type", "expected a list")
            return None
        result: list[str] = []
        for index, item in enumerate(value):
            parsed = self.string(item, f"{path}[{index}]")
            if parsed is not None:
                result.append(parsed)
        return result

    def require(self, value: Mapping[str, Any], key: str, path: str) -> Any:
        if key not in value:
            self.error(f"{path}.{key}", "required", "field is required")
            return None
        return value[key]

    def absolute_path(self, value: Any, path: str) -> Path | None:
        parsed = self.string(value, path)
        if parsed is None:
            return None
        if ABSOLUTE_ENV_OR_GLOB.search(parsed) or "\\" in parsed or any(part in {".", ".."} for part in Path(parsed).parts):
            self.error(path, "unsafe_path", "path must be explicit POSIX text without env vars, home expansion, or globs")
            return None
        candidate = Path(parsed)
        if not candidate.is_absolute():
            self.error(path, "absolute_path", "path must be absolute")
            return None
        return Path(*candidate.parts)

    def safe_subpath(self, value: Any, path: str) -> str | None:
        parsed = self.string(value, path)
        if parsed is None:
            return None
        posix = PurePosixPath(parsed)
        if posix.is_absolute() or parsed in {"", "."} or any(part in {"", ".", ".."} for part in posix.parts):
            self.error(path, "unsafe_subpath", "checkout_subpath must be a non-empty relative path without . or ..")
            return None
        return "/".join(posix.parts)

    @staticmethod
    def contained(child: Path, parent: Path) -> bool:
        try:
            child.relative_to(parent)
            return child != parent
        except ValueError:
            return False

    @staticmethod
    def overlap(left: Path, right: Path) -> bool:
        return left == right or _Validator.contained(left, right) or _Validator.contained(right, left)

    def unique_path(self, path: Path, path_name: str, seen: list[tuple[str, Path]], current: str) -> None:
        for previous, other in seen:
            if self.overlap(path, other):
                self.error(current, "path_overlap", f"overlaps {previous}")
        seen.append((current, path))

    def check_identifier(self, value: Any, path: str) -> str | None:
        parsed = self.string(value, path)
        if parsed is not None and not IDENTIFIER.fullmatch(parsed):
            self.error(path, "identifier", "must match lowercase DNS-style identifier syntax")
        return parsed

    def check_discord_id(self, value: Any, path: str) -> str | None:
        parsed = self.string(value, path)
        if parsed is not None and not DISCORD_ID.fullmatch(parsed):
            self.error(path, "discord_id", "must be a numeric Discord user/channel snowflake, not a logical agent ID")
        return parsed

    def validate(self, document: Any) -> dict[str, Any] | None:
        root = self.mapping(document, "$")
        if root is None:
            return None
        self.keys(root, {"version", "defaults", "agents"}, "$")
        version = root.get("version")
        if version != CATALOG_VERSION:
            self.error("$.version", "version", f"unsupported catalog version; expected {CATALOG_VERSION}")
        defaults = self.validate_defaults(root.get("defaults", {}))
        agents_value = self.require(root, "agents", "$")
        agents = self.mapping(agents_value, "$.agents")
        if agents is None:
            return None
        if not agents:
            self.error("$.agents", "empty", "at least one leader and one worker are required")

        normalized_agents: dict[str, Any] = {}
        roles: dict[str, str] = {}
        for agent_id in sorted(agents):
            if not isinstance(agent_id, str) or not IDENTIFIER.fullmatch(agent_id):
                self.error(f"$.agents.{agent_id}", "identifier", "agent ID must match lowercase DNS-style identifier syntax")
                continue
            role, normalized = self.validate_agent(agent_id, agents[agent_id], defaults)
            if role is not None:
                roles[agent_id] = role
            if normalized is not None:
                normalized_agents[agent_id] = normalized

        leaders = [agent_id for agent_id, role in roles.items() if role == "leader"]
        workers = [agent_id for agent_id, role in roles.items() if role in WORKER_ROLES]
        if len(leaders) != 1:
            self.error("$.agents", "leader_count", "exactly one leader is required")
        if not workers:
            self.error("$.agents", "worker_count", "at least one worker is required")
        self.validate_trust(roles, normalized_agents, leaders[0] if len(leaders) == 1 else None, workers)

        if self.errors:
            return None
        return {
            "version": CATALOG_VERSION,
            "defaults": defaults,
            "agents": normalized_agents,
        }

    def validate_defaults(self, value: Any) -> dict[str, str]:
        path = "$.defaults"
        defaults = self.mapping(value, path)
        if defaults is None:
            return {"base_branch": DEFAULT_BASE_BRANCH, "human_access": "deny", "bot_message_mode": "mentions"}
        self.keys(defaults, {"base_branch", "human_access", "bot_message_mode"}, path)
        base_branch = defaults.get("base_branch", DEFAULT_BASE_BRANCH)
        base_branch = self.string(base_branch, f"{path}.base_branch") or DEFAULT_BASE_BRANCH
        human_access = defaults.get("human_access", "deny")
        human_access = self.string(human_access, f"{path}.human_access") or "deny"
        if human_access != "deny":
            self.error(f"{path}.human_access", "policy", "default human access must be deny")
        bot_mode = defaults.get("bot_message_mode", "mentions")
        bot_mode = self.string(bot_mode, f"{path}.bot_message_mode") or "mentions"
        if bot_mode != "mentions":
            self.error(f"{path}.bot_message_mode", "policy", "default bot message mode must be mentions")
        return {"base_branch": base_branch, "human_access": human_access, "bot_message_mode": bot_mode}

    def validate_agent(self, agent_id: str, value: Any, defaults: Mapping[str, str]) -> tuple[str | None, dict[str, Any] | None]:
        path = f"$.agents.{agent_id}"
        agent = self.mapping(value, path)
        if agent is None:
            return None, None
        self.keys(agent, {"role", "runtime", "discord", "worktree", "repository_grants", "delivery", "egress_grants", "resources"}, path)
        role = self.string(self.require(agent, "role", path), f"{path}.role")
        if role not in ROLES:
            self.error(f"{path}.role", "role", "must be leader, researcher, developer, or reviewer")
            role = None
        runtime = self.validate_runtime(self.require(agent, "runtime", path), f"{path}.runtime")
        discord = self.validate_discord(self.require(agent, "discord", path), f"{path}.discord", role)
        worktree = self.validate_worktree(self.require(agent, "worktree", path), f"{path}.worktree")
        grants = self.validate_grants(self.require(agent, "repository_grants", path), f"{path}.repository_grants", worktree, defaults, agent_id)
        delivery = self.validate_delivery(self.require(agent, "delivery", path), f"{path}.delivery")
        egress = self.validate_egress(self.require(agent, "egress_grants", path), f"{path}.egress_grants")
        resources = self.validate_resources(self.require(agent, "resources", path), f"{path}.resources")
        if any(item is None for item in (runtime, discord, worktree, grants, delivery, egress, resources)):
            return role, None
        return role, {
            "role": role,
            "runtime": runtime,
            "discord": discord,
            "worktree": worktree,
            "repository_grants": grants,
            "delivery": delivery,
            "egress_grants": egress,
            "resources": resources,
        }

    def validate_runtime(self, value: Any, path: str) -> dict[str, Any] | None:
        runtime = self.mapping(value, path)
        if runtime is None:
            return None
        self.keys(runtime, {"command", "args", "model", "image"}, path)
        command = self.string(self.require(runtime, "command", path), f"{path}.command")
        args = self.list_of_strings(runtime.get("args", []), f"{path}.args")
        model = self.string(self.require(runtime, "model", path), f"{path}.model")
        image = self.string(self.require(runtime, "image", path), f"{path}.image")
        if command is not None and not re.fullmatch(r"[A-Za-z0-9._/+:-]+", command):
            self.error(f"{path}.command", "runtime_command", "must be one executable name/path; put flags in args and do not use shell syntax")
        if args is not None:
            for index, argument in enumerate(args):
                if any(character in argument for character in ("\0", "\r", "\n")):
                    self.error(f"{path}.args[{index}]", "runtime_arg", "must not contain control characters")
        for field, value in (("model", model), ("image", image)):
            if value is not None and any(character in value for character in ("\0", "\r", "\n")):
                self.error(f"{path}.{field}", "runtime_value", "must not contain control characters")
        if any(item is None for item in (command, args, model, image)):
            return None
        return {"command": command, "args": args, "model": model, "image": image}

    def validate_discord(self, value: Any, path: str, role: str | None) -> dict[str, Any] | None:
        discord = self.mapping(value, path)
        if discord is None:
            return None
        allowed = {"bot_secret_ref", "bot_user_id", "entry_channel_id", "work_channel_id", "allow_all_channels", "allow_all_users", "allowed_users", "allow_bot_messages", "allow_user_messages", "trusted_bot_ids"}
        self.keys(discord, allowed, path)
        secret_ref = self.string(self.require(discord, "bot_secret_ref", path), f"{path}.bot_secret_ref")
        if secret_ref is not None and not SECRET_REF.fullmatch(secret_ref):
            self.error(f"{path}.bot_secret_ref", "secret_ref", "must be a Kubernetes secret/key reference")
        elif secret_ref is not None and self.available_secret_refs is not None and secret_ref not in self.available_secret_refs:
            self.error(f"{path}.bot_secret_ref", "unresolved_ref", "Secret reference is not present in the supplied reference manifest")
        if secret_ref is not None and SECRET_REF.fullmatch(secret_ref):
            previous = self._secret_refs.get(secret_ref)
            if previous:
                self.error(f"{path}.bot_secret_ref", "duplicate_secret_ref", f"Discord bot Secret reference duplicates {previous}")
            else:
                self._secret_refs[secret_ref] = f"{path}.bot_secret_ref"
        bot_id = self.check_discord_id(self.require(discord, "bot_user_id", path), f"{path}.bot_user_id")
        entry = self.check_discord_id(discord.get("entry_channel_id"), f"{path}.entry_channel_id") if discord.get("entry_channel_id") is not None else None
        work = self.check_discord_id(discord.get("work_channel_id"), f"{path}.work_channel_id") if discord.get("work_channel_id") is not None else None
        if role == "leader":
            if entry is None:
                self.error(f"{path}.entry_channel_id", "role_field", "leader requires entry_channel_id")
            if work is not None:
                self.error(f"{path}.work_channel_id", "role_field", "leader cannot define work_channel_id")
        elif role in WORKER_ROLES:
            if work is None:
                self.error(f"{path}.work_channel_id", "role_field", "worker requires work_channel_id")
            if entry is not None:
                self.error(f"{path}.entry_channel_id", "role_field", "worker cannot define entry_channel_id")
        elif entry is None and work is None:
            self.error(path, "channel", "one role-appropriate Discord channel is required")
        for channel_path, channel in ((f"{path}.entry_channel_id", entry), (f"{path}.work_channel_id", work)):
            if channel is not None:
                previous = self._channels.get(channel)
                if previous:
                    self.error(channel_path, "duplicate_channel", f"duplicates {previous}")
                else:
                    self._channels[channel] = channel_path
        if bot_id is not None:
            previous = self._bots.get(bot_id)
            if previous:
                self.error(f"{path}.bot_user_id", "duplicate_bot", f"duplicates {previous}")
            else:
                self._bots[bot_id] = f"{path}.bot_user_id"
        allow_all_channels = discord.get("allow_all_channels")
        if allow_all_channels is not False:
            self.error(f"{path}.allow_all_channels", "default_deny", "must be explicitly false")
        allow_all_users = discord.get("allow_all_users")
        if allow_all_users is not False:
            self.error(f"{path}.allow_all_users", "default_deny", "must be explicitly false")
        allowed_users = self.list_of_strings(discord.get("allowed_users"), f"{path}.allowed_users")
        if allowed_users is None:
            allowed_users = []
        for index, user_id in enumerate(allowed_users):
            if not DISCORD_ID.fullmatch(user_id):
                self.error(f"{path}.allowed_users[{index}]", "discord_id", "must be a numeric Discord human user ID")
        if role in WORKER_ROLES and allowed_users:
            self.error(f"{path}.allowed_users", "human_access", "workers must reject human messages with an empty allowlist")
        if role == "leader" and not allowed_users:
            self.error(f"{path}.allowed_users", "human_access", "leader requires an explicit human allowlist")
        allow_bot_messages = discord.get("allow_bot_messages")
        if allow_bot_messages != "mentions":
            self.error(f"{path}.allow_bot_messages", "bot_policy", "must be mentions")
        allow_user_messages = discord.get("allow_user_messages")
        if allow_user_messages != "multibot-mentions":
            self.error(f"{path}.allow_user_messages", "user_policy", "must be multibot-mentions")
        trusted = self.list_of_strings(discord.get("trusted_bot_ids"), f"{path}.trusted_bot_ids")
        if trusted is None:
            trusted = []
        for index, trusted_id in enumerate(trusted):
            if not DISCORD_ID.fullmatch(trusted_id):
                self.error(f"{path}.trusted_bot_ids[{index}]", "discord_id", "must contain Discord bot user IDs, not logical agent IDs")
        return {
            "bot_secret_ref": secret_ref,
            "bot_user_id": bot_id,
            "entry_channel_id": entry,
            "work_channel_id": work,
            "allow_all_channels": False,
            "allow_all_users": False,
            "allowed_users": sorted(set(allowed_users)),
            "allow_bot_messages": "mentions",
            "allow_user_messages": "multibot-mentions",
            "trusted_bot_ids": sorted(set(trusted)),
        }

    def validate_worktree(self, value: Any, path: str) -> dict[str, Any] | None:
        worktree = self.mapping(value, path)
        if worktree is None:
            return None
        self.keys(worktree, {"path", "container_mount_path", "collection_roots"}, path)
        host_path = self.absolute_path(self.require(worktree, "path", path), f"{path}.path")
        mount_path = self.absolute_path(self.require(worktree, "container_mount_path", path), f"{path}.container_mount_path")
        roots_value = self.require(worktree, "collection_roots", path)
        roots = self.list_of_strings(roots_value, f"{path}.collection_roots")
        root_paths: list[Path] = []
        if roots is not None:
            for index, root in enumerate(roots):
                parsed = self.absolute_path(root, f"{path}.collection_roots[{index}]")
                if parsed is not None:
                    root_paths.append(parsed)
            for index, root in enumerate(root_paths):
                for previous_index, previous_root in enumerate(root_paths[:index]):
                    if self.overlap(root, previous_root):
                        self.error(
                            f"{path}.collection_roots[{index}]",
                            "path_overlap",
                            f"overlaps {path}.collection_roots[{previous_index}]",
                        )
            if self.check_paths:
                for index, root in enumerate(root_paths):
                    if not root.exists():
                        self.error(f"{path}.collection_roots[{index}]", "missing_path", "collection root does not exist")
            # Compare real paths as well as lexical paths.  A symlink inside a
            # collection/worktree root must not provide an escape hatch to an
            # unapproved directory.
            root_paths = [root.resolve(strict=False) for root in root_paths]
        if host_path is not None:
            host_path = host_path.resolve(strict=False)
            self.unique_path(host_path, "worktree", self._worktree_paths, f"{path}.path")
            for index, root in enumerate(root_paths):
                if self.overlap(host_path, root):
                    self.error(
                        f"{path}.path",
                        "source_boundary",
                        f"worktree must not overlap collection root {path}.collection_roots[{index}]",
                    )
        if mount_path is not None:
            self.unique_path(mount_path, "container mount", self._mount_paths, f"{path}.container_mount_path")
        if host_path is None or mount_path is None:
            return None
        return {"path": str(host_path), "container_mount_path": str(mount_path), "collection_roots": sorted(str(root) for root in root_paths)}

    def validate_grants(self, value: Any, path: str, worktree: Mapping[str, Any] | None, defaults: Mapping[str, str], agent_id: str) -> list[dict[str, Any]] | None:
        if not isinstance(value, list):
            self.error(path, "type", "expected a list")
            return None
        if not value:
            self.error(path, "empty", "each agent needs at least one exact repository grant")
            return None
        roots = [Path(root) for root in (worktree or {}).get("collection_roots", [])]
        normalized: list[dict[str, Any]] = []
        for index, raw in enumerate(value):
            item_path = f"{path}[{index}]"
            grant = self.mapping(raw, item_path)
            if grant is None:
                continue
            self.keys(grant, {"repository", "checkout_subpath", "access", "base_branch"}, item_path)
            repository = self.absolute_path(self.require(grant, "repository", item_path), f"{item_path}.repository")
            checkout = self.safe_subpath(self.require(grant, "checkout_subpath", item_path), f"{item_path}.checkout_subpath")
            access = self.string(self.require(grant, "access", item_path), f"{item_path}.access")
            if access not in ACCESS:
                self.error(f"{item_path}.access", "access", "must be read or write")
                access = None
            base_branch = self.string(grant.get("base_branch", defaults["base_branch"]), f"{item_path}.base_branch")
            if repository is not None:
                repository = repository.resolve(strict=False)
                matching_roots = [root for root in roots if self.contained(repository, root)]
                if len(matching_roots) != 1:
                    self.error(f"{item_path}.repository", "collection_boundary", "repository must belong to exactly one collection root")
                if self.check_paths and not repository.exists():
                    self.error(f"{item_path}.repository", "missing_path", "repository path does not exist")
                if self.check_git and repository.exists() and not (repository / ".git").exists():
                    self.error(f"{item_path}.repository", "not_git", "repository path must contain Git metadata")
                if checkout is not None:
                    key = (agent_id, str(repository), checkout)
                    previous = self._repos.get(key)
                    if previous:
                        self.error(f"{item_path}.checkout_subpath", "duplicate_checkout", f"duplicates {previous}")
                    self._repos[key] = item_path
                    for (old_agent, old_repo, old_checkout), previous in self._repos.items():
                        if old_agent == agent_id and old_repo == str(repository) and old_checkout != checkout and self._checkout_overlap(old_checkout, checkout):
                            self.error(f"{item_path}.checkout_subpath", "checkout_overlap", f"overlaps {previous}")
            if all(item is not None for item in (repository, checkout, access, base_branch)):
                normalized.append({"repository": str(repository), "checkout_subpath": checkout, "access": access, "base_branch": base_branch})
        return sorted(normalized, key=lambda item: (item["repository"], item["checkout_subpath"]))

    @staticmethod
    def _checkout_overlap(left: str, right: str) -> bool:
        left_parts, right_parts = left.split("/"), right.split("/")
        return left_parts == right_parts or left_parts[: len(right_parts)] == right_parts or right_parts[: len(left_parts)] == left_parts

    def validate_delivery(self, value: Any, path: str) -> dict[str, str] | None:
        delivery = self.mapping(value, path)
        if delivery is None:
            return None
        self.keys(delivery, {"gitlab_identity_ref"}, path)
        identity = self.string(self.require(delivery, "gitlab_identity_ref", path), f"{path}.gitlab_identity_ref")
        if identity is None:
            return None
        if SECRET_VALUE.search(identity) or "/" in identity or "\\" in identity:
            self.error(f"{path}.gitlab_identity_ref", "identity_ref", "must be a non-secret identity reference")
        elif self.available_identity_refs is not None and identity not in self.available_identity_refs:
            self.error(f"{path}.gitlab_identity_ref", "unresolved_ref", "GitLab identity reference is not present in the supplied reference manifest")
        return {"gitlab_identity_ref": identity}

    def validate_egress(self, value: Any, path: str) -> list[str] | None:
        grants = self.list_of_strings(value, path)
        if grants is None:
            return None
        if not grants:
            self.error(path, "empty", "egress grants must be explicit; use an empty list only after policy review")
        for index, grant in enumerate(grants):
            if grant == "*" or not EGRESS_NAME.fullmatch(grant):
                self.error(f"{path}[{index}]", "egress_grant", "must be a named, non-wildcard egress grant")
        return sorted(set(grants))

    def validate_resources(self, value: Any, path: str) -> dict[str, dict[str, str]] | None:
        resources = self.mapping(value, path)
        if resources is None:
            return None
        self.keys(resources, {"requests", "limits"}, path)
        result: dict[str, dict[str, str]] = {}
        for kind in ("requests", "limits"):
            values = self.mapping(self.require(resources, kind, path), f"{path}.{kind}")
            if values is None:
                continue
            self.keys(values, {"cpu", "memory"}, f"{path}.{kind}")
            parsed: dict[str, str] = {}
            for resource in ("cpu", "memory"):
                amount = self.string(self.require(values, resource, f"{path}.{kind}"), f"{path}.{kind}.{resource}")
                if amount is not None and not QUANTITY.fullmatch(amount):
                    self.error(f"{path}.{kind}.{resource}", "quantity", "must be a Kubernetes CPU or memory quantity")
                if amount is not None:
                    parsed[resource] = amount
            result[kind] = parsed
        return result if len(result) == 2 and all(len(values) == 2 for values in result.values()) else None

    def validate_trust(self, roles: Mapping[str, str], agents: Mapping[str, Any], leader: str | None, workers: Iterable[str]) -> None:
        if leader is None or leader not in agents:
            return
        leader_trusted = set(agents[leader].get("discord", {}).get("trusted_bot_ids", []))
        worker_bots = {agents[worker]["discord"]["bot_user_id"] for worker in workers if worker in agents and agents[worker].get("discord", {}).get("bot_user_id")}
        if leader_trusted != worker_bots:
            self.error(f"$.agents.{leader}.discord.trusted_bot_ids", "trust_topology", "leader must trust exactly every worker Discord bot user ID")
        leader_bot = agents[leader].get("discord", {}).get("bot_user_id")
        for worker in workers:
            if worker not in agents:
                continue
            trusted = set(agents[worker].get("discord", {}).get("trusted_bot_ids", []))
            if trusted != ({leader_bot} if leader_bot else set()):
                self.error(f"$.agents.{worker}.discord.trusted_bot_ids", "trust_topology", "worker may trust only the leader Discord bot user ID")


def validate_catalog(document: Any, *, check_paths: bool = True, check_git: bool = False, available_secret_refs: set[str] | None = None, available_identity_refs: set[str] | None = None) -> tuple[dict[str, Any] | None, list[Diagnostic]]:
    """Validate a loaded YAML document and return a stable normalized model."""

    validator = _Validator(
        check_paths=check_paths,
        check_git=check_git,
        available_secret_refs=available_secret_refs,
        available_identity_refs=available_identity_refs,
    )
    normalized = validator.validate(document)
    return normalized, sorted(validator.errors, key=lambda diagnostic: (diagnostic.path, diagnostic.code, diagnostic.message))


def load_catalog(path: str | Path, *, check_paths: bool = True, check_git: bool = False, available_secret_refs: set[str] | None = None, available_identity_refs: set[str] | None = None) -> tuple[dict[str, Any] | None, list[Diagnostic]]:
    """Load and validate a catalog without echoing parser input or secrets."""

    catalog_path = Path(path)
    try:
        with catalog_path.open("r", encoding="utf-8") as stream:
            document = load_yaml(stream)
    except (OSError, yaml.YAMLError) as exc:
        return None, [Diagnostic("$", "load", f"unable to load catalog: {type(exc).__name__}")]
    return validate_catalog(
        document,
        check_paths=check_paths,
        check_git=check_git,
        available_secret_refs=available_secret_refs,
        available_identity_refs=available_identity_refs,
    )


def load_reference_manifest(path: str | Path) -> tuple[set[str], set[str], list[Diagnostic]]:
    """Load names only from a local reference manifest; values are forbidden."""

    manifest_path = Path(path)
    try:
        with manifest_path.open("r", encoding="utf-8") as stream:
            document = load_yaml(stream)
    except (OSError, yaml.YAMLError) as exc:
        return set(), set(), [Diagnostic("$", "reference_manifest", f"unable to load reference manifest: {type(exc).__name__}")]
    if not isinstance(document, Mapping):
        return set(), set(), [Diagnostic("$", "reference_manifest", "reference manifest must be a mapping")]
    unknown = set(document) - {"secret_refs", "secrets", "identity_refs"}
    diagnostics = [Diagnostic(f"$.{key}", "unknown_field", "field is not part of the reference manifest") for key in sorted(unknown)]

    def names(key: str) -> set[str]:
        value = document.get(key, [])
        if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
            diagnostics.append(Diagnostic(f"$.{key}", "reference_manifest", "must be a list of non-empty names"))
            return set()
        return set(value)

    secret_refs = names("secret_refs")
    if "secrets" in document:
        values = document["secrets"]
        if not isinstance(values, Mapping) or not all(isinstance(item, str) and item.strip() for item in values):
            diagnostics.append(Diagnostic("$.secrets", "reference_manifest", "secrets must be a mapping of reference names to local values"))
        else:
            secret_refs.update(values)
    return secret_refs, names("identity_refs"), sorted(diagnostics, key=lambda diagnostic: (diagnostic.path, diagnostic.code, diagnostic.message))


def normalized_json(catalog: Mapping[str, Any]) -> str:
    """Serialize a normalized catalog with stable ordering for plans and tests."""

    return json.dumps(catalog, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
