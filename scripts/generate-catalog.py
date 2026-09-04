#!/usr/bin/env python3
"""Generate catalog.yaml, remotes.json, and the mirror source list from one table.

Four agents times a dozen repository grants is fifty-odd blocks of YAML.
Hand-editing that invites silent mistakes -- a wrong base_branch, a
checkout_subpath that collides, a read grant that should have been write.

The declarative input lives in a separate JSON file because it holds
operator-specific data: real Discord IDs and the paths of the repositories
being granted. That file is gitignored; this script is not.

Usage:
    python3 scripts/generate-catalog.py                 > config/catalog.yaml
    python3 scripts/generate-catalog.py --remotes       > config/remotes.json
    python3 scripts/generate-catalog.py --sources         # for mirror-sources.sh

    --input PATH    default: config/catalog-input.json
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

DEFAULT_INPUT = "config/catalog-input.json"
ROLES = ("leader", "researcher", "developer", "reviewer")


def load(path: str) -> dict:
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(
            f"找不到 {path}\n"
            f"請從範本複製：cp config/catalog-input.example.json {path}"
        )
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{path} 不是合法 JSON：{exc}")

    for key in ("home", "image", "mirror_root", "bots", "channels", "human_user_id", "grants"):
        if key not in document:
            raise SystemExit(f"{path} 缺少必要欄位：{key}")
    missing = [role for role in ROLES if role not in document["bots"]]
    if missing:
        raise SystemExit(f"{path} 的 bots 缺少角色：{', '.join(missing)}")
    if not document["grants"]:
        raise SystemExit(f"{path} 的 grants 是空的")
    return document


def checkout_subpath(root: str, repo: str) -> str:
    """Namespace each checkout by its collection so names cannot collide."""

    return f"{collection_of(root)}/{repo}"


def collection_of(root: str) -> str:
    return root.rstrip("/").rsplit("/", 1)[-1]


def collection_roots(grants: list[dict]) -> list[str]:
    seen: list[str] = []
    for grant in grants:
        if grant["root"] not in seen:
            seen.append(grant["root"])
    return seen


def emit_catalog(cfg: dict) -> str:
    grants = cfg["grants"]
    roots = collection_roots(grants)
    bots, channels = cfg["bots"], cfg["channels"]
    writers = set(cfg.get("writers", ["developer"]))
    egress = cfg.get("egress", {})
    resources = cfg.get("resources", {})
    default_res = resources.get("_default", {"cpu": "100m", "memory": "256Mi", "cpu_limit": "1", "memory_limit": "1Gi"})

    lines: list[str] = []
    add = lines.append
    add("# ============================================================================")
    add("# 由 scripts/generate-catalog.py 產生 —— 不要手動編輯")
    add("#")
    add(f"# 改 {DEFAULT_INPUT} 後重新產生：")
    add("#   python3 scripts/generate-catalog.py > config/catalog.yaml")
    add("#   python3 scripts/generate-catalog.py --remotes > config/remotes.json")
    add("#   bash scripts/mirror-sources.sh")
    add("#")
    add(f"# {len(grants)} 個 repo × {len(ROLES)} 個 agent = {len(grants) * len(ROLES)} 個掛載點")
    add(f"#   可寫：{', '.join(sorted(writers))}；其餘唯讀（由 kernel 掛載旗標強制）")
    add("# ============================================================================")
    add("version: 1")
    add("")
    add("defaults:")
    add(f"  base_branch: {cfg.get('default_base_branch', 'origin/develop')}")
    add("  human_access: deny")
    add("  bot_message_mode: mentions")
    add("")
    add("agents:")

    for role in ROLES:
        res = resources.get(role, default_res)
        access = "write" if role in writers else "read"
        trusted = [bots[r] for r in ROLES if r != "leader"] if role == "leader" else [bots["leader"]]

        add("")
        add(f"  {role}:")
        add(f"    role: {role}")
        add("    runtime:")
        add(f"      command: {cfg.get('command', 'openab-agent')}")
        add("      args: []")
        add(f"      model: {cfg.get('models', {}).get(role, f'model-{role}')}")
        add(f"      image: {cfg['image']}")
        add(f"      working_dir: {cfg.get('working_dir', '/home/agent')}")
        add("    discord:")
        add(f"      bot_secret_ref: discord-{role}/token")
        add(f'      bot_user_id: "{bots[role]}"')
        add(f'      {"entry" if role == "leader" else "work"}_channel_id: "{channels[role]}"')
        add("      allow_all_channels: false")
        add("      allow_all_users: false")
        add(f'      allowed_users: ["{cfg["human_user_id"]}"]' if role == "leader" else "      allowed_users: []")
        add("      allow_bot_messages: mentions")
        add("      allow_user_messages: multibot-mentions")
        add("      trusted_bot_ids:")
        for bot in trusted:
            add(f'        - "{bot}"')
        add("    worktree:")
        add(f"      path: {cfg['home']}/oab-agent-worktrees/{role}")
        add(f"      container_mount_path: /workspaces/{role}")
        add("      collection_roots:")
        for root in roots:
            add(f"        - {root}")
        add("    repository_grants:")
        current = None
        for grant in grants:
            if grant["root"] != current:
                add(f"      # --- {grant['root']}")
                current = grant["root"]
            add(f"      - repository: {grant['root']}/{grant['repo']}")
            add(f"        checkout_subpath: {checkout_subpath(grant['root'], grant['repo'])}")
            add(f"        access: {access}")
            add(f"        base_branch: {grant['base_branch']}")
        add("    delivery:")
        add(f"      gitlab_identity_ref: {cfg.get('gitlab_identity_ref', 'gitlab-bootstrap')}")
        add(f"    egress_grants: [{', '.join(egress.get(role, ['discord', 'model-provider']))}]")
        add("    resources:")
        add(f"      requests: {{cpu: {res['cpu']}, memory: {res['memory']}}}")
        add(f'      limits: {{cpu: "{res["cpu_limit"]}", memory: {res["memory_limit"]}}}')

    return "\n".join(lines) + "\n"


def emit_remotes(cfg: dict) -> str:
    """Map every granted repository to its local bare mirror.

    Generated from the same table as the catalog so the two cannot drift: a
    repository added to the catalog without a delivery remote fails
    worktree-materialize with 'missing configured GitLab remote'.
    """

    root = cfg["mirror_root"]
    remotes = {
        f"{grant['root']}/{grant['repo']}": f"file://{root}/{collection_of(grant['root'])}/{grant['repo']}.git"
        for grant in cfg["grants"]
    }
    return json.dumps(remotes, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def emit_sources(cfg: dict) -> str:
    """Tab-separated source→mirror pairs for scripts/mirror-sources.sh.

    The shell script used to keep its own copy of the repository list, which
    immediately drifted -- one repository was mirrored to a flat path while the
    generated remote expected it under a collection directory.
    """

    root = cfg["mirror_root"]
    return "".join(
        f"{grant['root']}/{grant['repo']}\t{root}/{collection_of(grant['root'])}/{grant['repo']}.git\n"
        for grant in cfg["grants"]
    )


if __name__ == "__main__":
    argv = sys.argv[1:]
    path = DEFAULT_INPUT
    if "--input" in argv:
        path = argv[argv.index("--input") + 1]
    config = load(path)

    if "--remotes" in argv:
        sys.stdout.write(emit_remotes(config))
    elif "--sources" in argv:
        sys.stdout.write(emit_sources(config))
    else:
        sys.stdout.write(emit_catalog(config))
