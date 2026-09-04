"""Render namespace-scoped Kubernetes isolation resources without applying them."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

import yaml


class KubernetesRenderError(ValueError):
    """Raised when Kubernetes isolation settings are unsafe."""


def catalog_revision(catalog: Mapping[str, Any]) -> str:
    """Return a deterministic, non-secret revision label for a normalized catalog."""

    payload = json.dumps(catalog, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def agent_service_account_name(agent_id: str) -> str:
    """Return a DNS-safe, collision-resistant ServiceAccount name."""

    _safe_name(agent_id)
    candidate = f"oab-agent-{agent_id}"
    if len(candidate) <= 63:
        return candidate
    digest = hashlib.sha256(agent_id.encode("utf-8")).hexdigest()[:8]
    return f"oab-agent-{agent_id[:44]}-{digest}"


def agent_role_name(agent_id: str) -> str:
    return f"{agent_service_account_name(agent_id)}-role"[:63].rstrip("-")


def agent_role_binding_name(agent_id: str) -> str:
    return f"{agent_service_account_name(agent_id)}-binding"[:63].rstrip("-")


#: Resources that only a cluster-bootstrap identity may create.  The
#: namespace-scoped ``oab-control-deployer`` Role deliberately grants no
#: namespace or RBAC access, so a confirmed deploy must not try to re-apply
#: these -- they are installed once, out of band, before the first deploy.
BOOTSTRAP_ONLY_KINDS = frozenset({"Namespace", "Role", "RoleBinding"})

#: How agents are allowed to reach the network.
#:
#: ``proxy-only`` is the intended end state: nothing leaves the namespace
#: except through a domain-filtering proxy in ``oab-egress``.  It requires
#: that proxy to exist *and* that the workload honour it -- OpenAB's Discord
#: gateway calls ``tokio_tungstenite::connect_async`` directly and has no
#: proxy support, so this mode currently leaves agents with no usable path
#: out.  See docs/架構說明.md.
#:
#: ``public-tls`` is the documented interim: public HTTPS only, with private
#: ranges and the cloud metadata address still denied, so an agent cannot
#: reach cluster-internal services or steal instance credentials.  It does
#: not give per-domain control -- an agent may reach any public HTTPS host.
EGRESS_MODES = ("proxy-only", "public-tls")

#: Denied even in ``public-tls``: RFC1918, link-local (which covers the
#: 169.254.169.254 metadata endpoint), loopback, and CGNAT.
_PRIVATE_RANGES = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "127.0.0.0/8", "100.64.0.0/10")


def render_k8s_manifests(
    catalog: Mapping[str, Any],
    *,
    namespace: str = "oab-agents",
    proxy_namespace: str = "oab-egress",
    proxy_selector: Mapping[str, str] | None = None,
    proxy_port: int = 8080,
    deployer_scoped: bool = False,
    egress_mode: str = "proxy-only",
) -> list[dict[str, Any]]:
    """Render one non-privileged SA per agent and namespace isolation policy.

    With ``deployer_scoped`` the bootstrap-only resources are omitted, leaving
    exactly what the namespace-scoped deployer identity is permitted to apply.
    """

    _safe_name(namespace)
    _safe_name(proxy_namespace)
    if not 1 <= proxy_port <= 65535:
        raise KubernetesRenderError("proxy port must be between 1 and 65535")
    if egress_mode not in EGRESS_MODES:
        raise KubernetesRenderError(f"egress mode must be one of: {', '.join(EGRESS_MODES)}")
    selector = dict(proxy_selector or {"app.kubernetes.io/name": "oab-egress-proxy"})
    if not selector or any(not isinstance(key, str) or not isinstance(value, str) or not key or not value for key, value in selector.items()):
        raise KubernetesRenderError("proxy selector must contain non-empty string labels")
    manifests: list[dict[str, Any]] = [
        {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {
                "name": namespace,
                "labels": {
                    # HostPath is deliberately restricted to exact agent
                    # checkouts by the renderer.  PSA warn/audit provide
                    # visibility without silently blocking that explicit
                    # exception; enforcement is an operator decision.
                    "pod-security.kubernetes.io/audit": "restricted",
                    "pod-security.kubernetes.io/warn": "restricted",
                    "oab-agents.io/hostpath-policy": "narrow-agent-worktrees",
                },
            },
        }
    ]
    revision = catalog_revision(catalog)
    for agent_id in sorted(catalog["agents"]):
        _safe_name(agent_id)
        service_account = agent_service_account_name(agent_id)
        manifests.append(
            {
                "apiVersion": "v1",
                "kind": "ServiceAccount",
                "metadata": {
                    "name": service_account,
                    "namespace": namespace,
                    "labels": {"oab-agents.io/agent": agent_id, "oab-agents.io/catalog-revision": revision},
                },
                "automountServiceAccountToken": False,
            }
        )
        manifests.extend(
            [
                {
                    "apiVersion": "rbac.authorization.k8s.io/v1",
                    "kind": "Role",
                    "metadata": {
                        "name": agent_role_name(agent_id),
                        "namespace": namespace,
                        "labels": {"oab-agents.io/agent": agent_id, "oab-agents.io/catalog-revision": revision},
                    },
                    # An agent runtime has no Kubernetes API permissions by
                    # default.  Worktree access is a filesystem mount, not
                    # an RBAC grant.
                    "rules": [],
                },
                {
                    "apiVersion": "rbac.authorization.k8s.io/v1",
                    "kind": "RoleBinding",
                    "metadata": {
                        "name": agent_role_binding_name(agent_id),
                        "namespace": namespace,
                        "labels": {"oab-agents.io/agent": agent_id, "oab-agents.io/catalog-revision": revision},
                    },
                    "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": agent_role_name(agent_id)},
                    "subjects": [{"kind": "ServiceAccount", "name": service_account, "namespace": namespace}],
                },
            ]
        )
    deployer = "oab-control-deployer"
    manifests.extend(
        [
            {
                "apiVersion": "v1",
                "kind": "ServiceAccount",
                "metadata": {"name": deployer, "namespace": namespace, "labels": {"oab-agents.io/component": "deployer", "oab-agents.io/catalog-revision": revision}},
                "automountServiceAccountToken": False,
            },
            {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "Role",
                "metadata": {"name": deployer, "namespace": namespace, "labels": {"oab-agents.io/component": "deployer", "oab-agents.io/catalog-revision": revision}},
                "rules": [
                    {"apiGroups": [""], "resources": ["configmaps", "persistentvolumeclaims", "serviceaccounts"], "verbs": ["get", "list", "watch", "create", "update", "patch", "delete"]},
                    {"apiGroups": ["apps"], "resources": ["deployments"], "verbs": ["get", "list", "watch", "create", "update", "patch", "delete"]},
                    # ReplicaSets are created by the Deployments above, not by
                    # this identity, so they are read-only here.  Without them
                    # `kubectl rollout history` fails while `rollout restart`
                    # and `rollout status` succeed, which is an inconsistency
                    # rather than a boundary.
                    {"apiGroups": ["apps"], "resources": ["replicasets"], "verbs": ["get", "list", "watch"]},
                    {"apiGroups": ["networking.k8s.io"], "resources": ["networkpolicies"], "verbs": ["get", "list", "watch", "create", "update", "patch", "delete"]},
                    # Read-only observation of what this identity deployed.
                    #
                    # The rule for what belongs here: the deployer may *read*
                    # the objects its own Deployments produce, because an
                    # identity that can restart a workload but cannot see the
                    # Pods or the events explaining a failure is a blind spot,
                    # not a boundary.  It may not write them -- Pods change
                    # only through the Deployment the catalog declares.
                    #
                    # ``pods/exec`` and ``pods/log`` stay out no matter how
                    # convenient they are.  Agent containers hold their Discord
                    # bot token as a secretKeyRef environment variable, so a
                    # shell inside the Pod -- or a log stream that echoes it --
                    # would hand this identity exactly the Secret values the
                    # rules above are written to deny.  Use the bootstrap
                    # identity for exec and logs.
                    {"apiGroups": [""], "resources": ["pods", "events"], "verbs": ["get", "list", "watch"]},
                ],
            },
            {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "RoleBinding",
                "metadata": {"name": deployer, "namespace": namespace, "labels": {"oab-agents.io/component": "deployer", "oab-agents.io/catalog-revision": revision}},
                "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": deployer},
                "subjects": [{"kind": "ServiceAccount", "name": deployer, "namespace": namespace}],
            },
            {
                "apiVersion": "v1",
                "kind": "ServiceAccount",
                "metadata": {"name": "oab-control-secret-materializer", "namespace": namespace, "labels": {"oab-agents.io/component": "secret-materializer", "oab-agents.io/catalog-revision": revision}},
                "automountServiceAccountToken": False,
            },
            {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "Role",
                "metadata": {"name": "oab-control-secret-materializer", "namespace": namespace, "labels": {"oab-agents.io/component": "secret-materializer", "oab-agents.io/catalog-revision": revision}},
                # This identity can write selected Secret objects during an
                # explicit rotation, but has no get/list/watch permission and
                # therefore cannot read existing Secret values.
                "rules": [{"apiGroups": [""], "resources": ["secrets"], "verbs": ["create", "update", "patch"]}],
            },
            {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "RoleBinding",
                "metadata": {"name": "oab-control-secret-materializer", "namespace": namespace, "labels": {"oab-agents.io/component": "secret-materializer", "oab-agents.io/catalog-revision": revision}},
                "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": "oab-control-secret-materializer"},
                "subjects": [{"kind": "ServiceAccount", "name": "oab-control-secret-materializer", "namespace": namespace}],
            },
        ]
    )
    manifests.append(
        {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {"name": "oab-agents-default-deny", "namespace": namespace},
            "spec": {
                "podSelector": {},
                "policyTypes": ["Ingress", "Egress"],
                "ingress": [],
                "egress": [],
            },
        }
    )
    manifests.append(
        {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {"name": "oab-agents-allow-dns-and-egress-proxy", "namespace": namespace},
            "spec": {
                "podSelector": {},
                "policyTypes": ["Egress"],
                "egress": [
                    {
                        "to": [
                            {
                                "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
                                "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
                            }
                        ],
                        "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}],
                    },
                    {
                        "to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": proxy_namespace}}, "podSelector": {"matchLabels": selector}}],
                        "ports": [{"protocol": "TCP", "port": proxy_port}],
                    },
                ],
            },
        }
    )
    if egress_mode == "public-tls":
        manifests.append(
            {
                "apiVersion": "networking.k8s.io/v1",
                "kind": "NetworkPolicy",
                "metadata": {
                    "name": "oab-agents-allow-public-tls",
                    "namespace": namespace,
                    "annotations": {
                        # Say plainly, in the cluster itself, that this is a
                        # deliberate interim and what it does not give you.
                        "oab-agents.io/egress-mode": "public-tls",
                        "oab-agents.io/rationale": (
                            "Interim: OpenAB's Discord gateway cannot use an HTTP proxy, so proxy-only "
                            "leaves agents with no network path. Public HTTPS is allowed; private ranges "
                            "and the metadata address stay denied. This grants no per-domain control."
                        ),
                    },
                },
                "spec": {
                    "podSelector": {},
                    "policyTypes": ["Egress"],
                    "egress": [
                        {
                            "to": [{"ipBlock": {"cidr": "0.0.0.0/0", "except": list(_PRIVATE_RANGES)}}],
                            "ports": [{"protocol": "TCP", "port": 443}],
                        }
                    ],
                },
            }
        )
    if deployer_scoped:
        return [manifest for manifest in manifests if manifest["kind"] not in BOOTSTRAP_ONLY_KINDS]
    return manifests


def render_k8s_yaml(catalog: Mapping[str, Any], **kwargs: Any) -> str:
    """Serialize Kubernetes resources as deterministic multi-document YAML."""

    manifests = render_k8s_manifests(catalog, **kwargs)
    return "\n---\n".join(yaml.safe_dump(item, allow_unicode=True, sort_keys=False).rstrip() for item in manifests) + "\n"


def _safe_name(value: str) -> None:
    if not isinstance(value, str) or not value or len(value) > 63 or not all(char.isalnum() or char == "-" for char in value) or value[0] == "-" or value[-1] == "-":
        raise KubernetesRenderError("namespace, proxy namespace, and agent IDs must be DNS labels")
