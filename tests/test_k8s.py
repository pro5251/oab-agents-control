from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

import yaml

from oab_control.catalog import validate_catalog
from oab_control.k8s import KubernetesRenderError, agent_service_account_name, catalog_revision, render_k8s_manifests, render_k8s_yaml
from test_catalog import catalog


class KubernetesRenderTests(unittest.TestCase):
    def normalized(self) -> dict:
        with tempfile.TemporaryDirectory() as directory:
            value, diagnostics = validate_catalog(catalog(Path(directory)))
        self.assertEqual(diagnostics, [])
        assert value is not None
        return value

    def test_renders_one_sa_per_agent_and_default_deny_policies(self) -> None:
        catalog = self.normalized()
        manifests = render_k8s_manifests(catalog, namespace="oab-agents-test", proxy_port=18080)
        namespaces = [item for item in manifests if item["kind"] == "Namespace"]
        service_accounts = [item for item in manifests if item["kind"] == "ServiceAccount"]
        roles = [item for item in manifests if item["kind"] == "Role"]
        role_bindings = [item for item in manifests if item["kind"] == "RoleBinding"]
        policies = [item for item in manifests if item["kind"] == "NetworkPolicy"]
        self.assertEqual(len(namespaces), 1)
        self.assertEqual(namespaces[0]["metadata"]["name"], "oab-agents-test")
        self.assertEqual(len(service_accounts), 6)
        self.assertEqual(len(roles), 6)
        self.assertEqual(len(role_bindings), 6)
        self.assertTrue(all(item["automountServiceAccountToken"] is False for item in service_accounts))
        self.assertEqual(next(item for item in roles if item["metadata"]["name"] == "oab-control-deployer")["rules"][0]["resources"], ["configmaps", "persistentvolumeclaims", "serviceaccounts"])
        self.assertNotIn("secrets", str(next(item for item in roles if item["metadata"]["name"] == "oab-control-deployer")["rules"]))
        self.assertTrue(all(item["metadata"]["labels"]["oab-agents.io/catalog-revision"] == catalog_revision(catalog) for item in service_accounts))
        self.assertEqual(len(policies), 2)
        deny = next(item for item in policies if item["metadata"]["name"] == "oab-agents-default-deny")
        self.assertEqual(deny["spec"]["ingress"], [])
        self.assertEqual(deny["spec"]["egress"], [])
        allow = next(item for item in policies if item["metadata"]["name"].endswith("allow-dns-and-egress-proxy"))
        self.assertEqual(allow["spec"]["egress"][1]["ports"][0]["port"], 18080)
        self.assertEqual(
            allow["spec"]["egress"][1]["to"][0]["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"],
            "oab-egress",
        )

    def test_yaml_is_multi_document_and_bad_proxy_values_fail(self) -> None:
        rendered = render_k8s_yaml(self.normalized())
        self.assertEqual(rendered.count("apiVersion:"), 21)
        documents = list(yaml.safe_load_all(rendered))
        self.assertEqual(len(documents), 21)
        with self.assertRaises(KubernetesRenderError):
            render_k8s_manifests(self.normalized(), proxy_port=0)
        with self.assertRaises(KubernetesRenderError):
            render_k8s_manifests(self.normalized(), proxy_selector={"": "unsafe"})

    def test_long_agent_id_gets_bounded_service_account_name(self) -> None:
        name = agent_service_account_name("a" * 63)
        self.assertLessEqual(len(name), 63)
        self.assertTrue(name.startswith("oab-agent-"))
        self.assertEqual(name, agent_service_account_name("a" * 63))

    def test_default_egress_mode_allows_nothing_beyond_the_proxy(self) -> None:
        names = {item["metadata"]["name"] for item in render_k8s_manifests(self.normalized())}
        self.assertNotIn("oab-agents-allow-public-tls", names)
        self.assertIn("oab-agents-default-deny", names)

    def test_public_tls_mode_allows_443_but_still_denies_private_and_metadata(self) -> None:
        manifests = render_k8s_manifests(self.normalized(), egress_mode="public-tls")
        policy = next(item for item in manifests if item["metadata"]["name"] == "oab-agents-allow-public-tls")
        rule = policy["spec"]["egress"][0]
        self.assertEqual(rule["ports"], [{"protocol": "TCP", "port": 443}])
        block = rule["to"][0]["ipBlock"]
        self.assertEqual(block["cidr"], "0.0.0.0/0")
        # Cluster-internal movement and the cloud metadata endpoint stay denied.
        for denied in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16"):
            self.assertIn(denied, block["except"])
        # The widening has to explain itself where an operator will see it.
        self.assertIn("oab-agents.io/rationale", policy["metadata"]["annotations"])
        # default-deny is not removed; this policy is additive.
        names = {item["metadata"]["name"] for item in manifests}
        self.assertIn("oab-agents-default-deny", names)

    def test_public_tls_egress_survives_the_deployer_scoped_filter(self) -> None:
        """NetworkPolicy is deployer-applicable, so deploy must still carry it."""

        manifests = render_k8s_manifests(self.normalized(), egress_mode="public-tls", deployer_scoped=True)
        names = {item["metadata"]["name"] for item in manifests}
        self.assertIn("oab-agents-allow-public-tls", names)

    def test_unknown_egress_mode_is_rejected(self) -> None:
        with self.assertRaises(KubernetesRenderError):
            render_k8s_manifests(self.normalized(), egress_mode="allow-everything")


if __name__ == "__main__":
    unittest.main()
