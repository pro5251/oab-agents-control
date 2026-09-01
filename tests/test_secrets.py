from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

import yaml

from oab_control.secrets import SecretError, load_secret_values, materialize_secrets, render_secret_manifests


class SecretTests(unittest.TestCase):
    def test_materialization_groups_refs_but_requires_redacted_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secrets.yaml"
            path.write_text(yaml.safe_dump({"secrets": {"discord-a/token": "value-a", "gitlab-bootstrap/token": "value-b"}}), encoding="utf-8")
            path.chmod(0o600)
            values = load_secret_values(path)
            manifests = render_secret_manifests(values, namespace="oab-agents")
            applied: list[list[dict]] = []
            result = materialize_secrets(
                ["discord-a/token", "gitlab-bootstrap/token"],
                values_file=path,
                namespace="oab-agents",
                apply=lambda rendered: applied.append(rendered) or "applied 2 Secret objects",
            )
        self.assertEqual(result, "applied 2 Secret objects")
        self.assertEqual(len(manifests), 2)
        self.assertEqual(len(applied[0]), 2)
        self.assertEqual(applied[0][0]["metadata"]["name"], "discord-a")

    def test_missing_ref_and_secret_in_apply_status_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secrets.yaml"
            path.write_text("secrets:\n  discord-a/token: value-a\n", encoding="utf-8")
            path.chmod(0o600)
            with self.assertRaises(SecretError):
                materialize_secrets(["discord-a/token", "missing/key"], values_file=path, namespace="oab-agents", apply=lambda _: "not called")
            with self.assertRaises(SecretError):
                materialize_secrets(["discord-a/token"], values_file=path, namespace="oab-agents", apply=lambda _: "value-a leaked")

    def test_invalid_reference_shape_is_rejected(self) -> None:
        with self.assertRaises(SecretError):
            render_secret_manifests({"Bad_Name/token": "value"}, namespace="oab-agents")
        with self.assertRaises(SecretError):
            render_secret_manifests({"valid/not a key": "value"}, namespace="oab-agents")

    def test_local_secret_values_require_owner_only_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secrets.yaml"
            path.write_text("secrets:\n  discord-a/token: value-a\n", encoding="utf-8")
            path.chmod(0o644)
            with self.assertRaises(SecretError):
                load_secret_values(path)


if __name__ == "__main__":
    unittest.main()
