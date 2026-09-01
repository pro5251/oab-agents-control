from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

import yaml

from oab_control.preflight import collect_preflight
from test_environment import environment


class PreflightTests(unittest.TestCase):
    def test_reports_ready_without_exposing_kubeconfig_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for path in ("control", "coordination", "repositories", "worktrees", "k3s", "backups", "chart"):
                (root / path).mkdir()
            (root / "catalog.yaml").write_text("version: 1\n", encoding="utf-8")
            (root / "chart" / "Chart.yaml").write_text("apiVersion: v2\nname: test\nversion: 0.1.0\n", encoding="utf-8")
            deployer = root / "deployer.yaml"
            materializer = root / "materializer.yaml"
            deployer.write_text("apiVersion: v1\n", encoding="utf-8")
            materializer.write_text("apiVersion: v1\n", encoding="utf-8")
            contract = root / "environment.yaml"
            contract.write_text(yaml.safe_dump(environment(root), sort_keys=False), encoding="utf-8")
            result = collect_preflight(
                contract,
                chart_path=root / "chart",
                which=lambda _: "/usr/bin/tool",
                environ={"KUBECONFIG": str(deployer), "OAB_SECRET_MATERIALIZER_KUBECONFIG": str(materializer)},
            )
        self.assertTrue(result["ready"])
        self.assertTrue(result["read_only"])
        self.assertTrue(result["kubeconfigs"]["distinct_paths"])
        self.assertNotIn("resolved_path", result["kubeconfigs"]["deployer"])
        self.assertNotIn(str(deployer), str(result))

    def test_rejects_shared_or_missing_kubeconfig_without_reading_contents(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            contract = root / "environment.yaml"
            document = environment(root)
            document["status"] = "bootstrap-pending"
            document["pending_decisions"] = [{"key": "operator.input", "owner": "operator", "placeholder_policy": "fill later"}]
            contract.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
            result = collect_preflight(
                contract,
                chart_path=root / "missing-chart",
                which=lambda _: None,
                environ={"KUBECONFIG": str(root / "same.yaml"), "OAB_SECRET_MATERIALIZER_KUBECONFIG": str(root / "same.yaml")},
            )
        self.assertFalse(result["ready"])
        self.assertFalse(result["kubeconfigs"]["distinct_paths"])
        self.assertFalse(result["kubeconfigs"]["deployer"]["file_exists"])
        self.assertTrue(result["contract"]["diagnostics"])


if __name__ == "__main__":
    unittest.main()
