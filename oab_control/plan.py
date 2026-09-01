"""Pure catalog-to-plan projection.

The plan is deliberately data-only.  Applying it belongs to a later command
with a separate deployer identity and an explicit human confirmation.
"""

from __future__ import annotations

from typing import Any, Mapping

from .k8s import agent_service_account_name, catalog_revision, render_k8s_manifests


def render_plan(catalog: Mapping[str, Any], *, namespace: str = "oab-agents") -> dict[str, Any]:
    """Return a stable, secret-free deployment plan for a normalized catalog."""

    workloads: list[dict[str, Any]] = []
    revision = catalog_revision(catalog)
    for agent_id, agent in sorted(catalog["agents"].items()):
        worktree = agent["worktree"]
        mounts = []
        for grant in agent["repository_grants"]:
            subpath = grant["checkout_subpath"]
            mounts.append(
                {
                    "host_path": f"{worktree['path']}/{subpath}",
                    "mount_path": f"{worktree['container_mount_path']}/{subpath}",
                    "read_only": grant["access"] == "read",
                    "repository": grant["repository"],
                }
            )
        discord = agent["discord"]
        workloads.append(
            {
                "agent_id": agent_id,
                "catalog_revision": revision,
                "role": agent["role"],
                # The upstream chart uses each catalog ID as ``nameOverride``;
                # keeping the workload/PVC names identical makes the plan
                # directly comparable with Helm's rendered resources.
                "workload": agent_id,
                "service_account": agent_service_account_name(agent_id),
                "runtime_pvc": agent_id,
                "image": agent["runtime"]["image"],
                "command": agent["runtime"]["command"],
                "args": agent["runtime"]["args"],
                "model": agent["runtime"]["model"],
                "worktree_mounts": mounts,
                "discord": {
                    "bot_secret_ref": discord["bot_secret_ref"],
                    "bot_user_id": discord["bot_user_id"],
                    "entry_channel_id": discord["entry_channel_id"],
                    "work_channel_id": discord["work_channel_id"],
                    "allow_all_channels": discord["allow_all_channels"],
                    "allow_all_users": discord["allow_all_users"],
                    "allowed_users": discord["allowed_users"],
                    "allow_bot_messages": discord["allow_bot_messages"],
                    "allow_user_messages": discord["allow_user_messages"],
                    "trusted_bot_ids": discord["trusted_bot_ids"],
                },
                "gitlab_identity_ref": agent["delivery"]["gitlab_identity_ref"],
                "egress_grants": agent["egress_grants"],
                "resources": agent["resources"],
            }
        )
    k8s_manifests = render_k8s_manifests(catalog, namespace=namespace)
    return {
        "namespace": namespace,
        "catalog_version": catalog["version"],
        "catalog_revision": revision,
        "default_base_branch": catalog["defaults"]["base_branch"],
        "workloads": workloads,
        "rbac": {
            "service_accounts": [item["service_account"] for item in workloads],
            "agent_cluster_admin": False,
            "deployer_is_separate": True,
            "deployer_service_account": "oab-control-deployer",
            "secret_materializer_service_account": "oab-control-secret-materializer",
            "secret_read_allowed_to_agents": False,
        },
        "network_policy": {
            "default_deny_ingress": True,
            "default_deny_egress": True,
            "agent_egress_proxy_only": True,
            "proxy_namespace": "oab-egress",
        },
        "kubernetes_resources": {
            "namespaces": [manifest["metadata"]["name"] for manifest in k8s_manifests if manifest["kind"] == "Namespace"],
            "service_accounts": [manifest["metadata"]["name"] for manifest in k8s_manifests if manifest["kind"] == "ServiceAccount"],
            "roles": [manifest["metadata"]["name"] for manifest in k8s_manifests if manifest["kind"] == "Role"],
            "role_bindings": [manifest["metadata"]["name"] for manifest in k8s_manifests if manifest["kind"] == "RoleBinding"],
            "network_policies": [manifest["metadata"]["name"] for manifest in k8s_manifests if manifest["kind"] == "NetworkPolicy"],
            "agent_service_accounts_have_no_token_mount": True,
            "deployer_cannot_read_secrets": True,
        },
        "apply": {"requires_human_confirmation": True, "mutates_cluster": False},
    }
