import unittest

from oab_control.runtime_conformance import managed_codex_config_digest, observe_codex_features


class RuntimeConformanceTests(unittest.TestCase):
    def test_false_feature_with_current_digests_is_conformant(self):
        result = observe_codex_features("multi_agent stable false\n", image_digest="sha256:image", config_digest=managed_codex_config_digest())
        self.assertEqual(result["leader_subagents"], "conformant")

    def test_true_or_stale_config_is_non_conformant(self):
        result = observe_codex_features("multi_agent stable true\n", image_digest="sha256:image", config_digest="stale")
        self.assertEqual(result["leader_subagents"], "non_conformant")
