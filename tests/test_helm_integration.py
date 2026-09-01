from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import yaml

from oab_control.catalog import validate_catalog
from oab_control.renderer import render_openab_values_yaml
from test_catalog import catalog


class HelmIntegrationTests(unittest.TestCase):
    """Smoke-test the generated values against the checked-out upstream chart."""

    def test_upstream_chart_renders_only_catalog_agents(self) -> None:
        helm = shutil.which("helm") or str(Path.home() / ".local/bin/helm")
        chart = Path(__file__).resolve().parents[2] / "openab" / "charts" / "openab"
        if not Path(helm).is_file() or not chart.is_dir():
            self.skipTest("local helm binary or sibling OpenAB chart is unavailable")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            normalized, diagnostics = validate_catalog(catalog(root))
            self.assertEqual(diagnostics, [])
            assert normalized is not None
            values = root / "values.yaml"
            values.write_text(render_openab_values_yaml(normalized), encoding="utf-8")
            rendered = subprocess.run(
                [helm, "template", "oab-agents", str(chart), "--namespace", "oab-agents", "--values", str(values)],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
        documents = [item for item in yaml.safe_load_all(rendered) if item]
        self.assertEqual({item["kind"] for item in documents}, {"ConfigMap", "Deployment", "PersistentVolumeClaim"})
        self.assertEqual(len([item for item in documents if item["kind"] == "Deployment"]), 4)
        self.assertEqual(len([item for item in documents if item["kind"] == "ConfigMap"]), 4)
        self.assertEqual(len([item for item in documents if item["kind"] == "PersistentVolumeClaim"]), 4)
        names = {item["metadata"]["name"] for item in documents}
        self.assertNotIn("kiro", names)
        self.assertTrue({"leader", "researcher", "developer", "reviewer"}.issubset(names))
        for deployment in (item for item in documents if item["kind"] == "Deployment"):
            self.assertTrue(deployment["spec"]["template"]["spec"]["securityContext"]["runAsNonRoot"])
            self.assertEqual(deployment["spec"]["template"]["spec"]["securityContext"]["seccompProfile"]["type"], "RuntimeDefault")
            self.assertTrue(deployment["spec"]["template"]["spec"]["containers"][0]["securityContext"]["readOnlyRootFilesystem"])
            self.assertFalse(deployment["spec"]["template"]["spec"]["containers"][0]["securityContext"]["allowPrivilegeEscalation"])
            self.assertEqual(deployment["spec"]["template"]["spec"]["containers"][0]["securityContext"]["capabilities"]["drop"], ["ALL"])


if __name__ == "__main__":
    unittest.main()
