from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
import json
import io
from contextlib import redirect_stdout

from oab_control.backup import BackupError, COMPONENTS, LocalBackup
from oab_control.cli import main


class BackupTests(unittest.TestCase):
    def sources(self, root: Path) -> dict[str, Path]:
        root.mkdir(parents=True, exist_ok=True)
        paths = {
            "catalog": root / "catalog.yaml",
            "coordination_repository": root / "coordination",
            "agent_worktrees_root": root / "worktrees",
            "k3s_state": root / "k3s",
        }
        paths["catalog"].write_text("version: 1\n", encoding="utf-8")
        for component in COMPONENTS[1:]:
            paths[component].mkdir()
            (paths[component] / "state.txt").write_text(component, encoding="utf-8")
        (paths["coordination_repository"] / "secrets.yaml").write_text("discord-a/token: must-not-backup", encoding="utf-8")
        return paths

    def test_backup_is_checksum_verified_and_excludes_secret_values_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sources = self.sources(root / "sources")
            backup_root = root / "backups"
            result = LocalBackup(backup_root).create(
                sources,
                encryption_attestation="operator verified encrypted NAS mount",
                backup_id="backup-20260901T010203Z-deadbeef",
            )
            manifest = LocalBackup(backup_root).verify(result.path)
            self.assertFalse((Path(result.path) / "coordination_repository" / "secrets.yaml").exists())
            self.assertIn("coordination_repository/secrets.yaml", manifest["excluded_paths"])
            self.assertFalse(manifest["secret_values_included"])
            self.assertNotIn("must-not-backup", (Path(result.path) / "manifest.json").read_text(encoding="utf-8"))

    def test_restore_requires_confirmation_and_reconstructs_clean_destinations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sources = self.sources(root / "sources")
            backup = LocalBackup(root / "backups").create(sources, encryption_attestation="encrypted external disk").path
            destinations = {component: root / "restored" / component for component in COMPONENTS}
            destinations["catalog"] = root / "restored" / "catalog.yaml"
            with self.assertRaises(BackupError):
                LocalBackup(root / "backups").restore(backup, destinations, confirmed=False)
            restored = LocalBackup(root / "backups").restore(backup, destinations, confirmed=True)
            self.assertEqual(set(restored["restored_components"]), set(COMPONENTS))
            self.assertEqual(destinations["catalog"].read_text(encoding="utf-8"), "version: 1\n")
            self.assertFalse((destinations["coordination_repository"] / "secrets.yaml").exists())

    def test_tamper_and_unsafe_target_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sources = self.sources(root / "sources")
            with self.assertRaises(BackupError):
                LocalBackup(root / "sources" / "coordination" / "nested-backups").create(sources, encryption_attestation="encrypted disk")
            result = LocalBackup(root / "backups").create(sources, encryption_attestation="encrypted disk")
            entry = next(item for item in result.manifest["files"] if item["path"].endswith("/state.txt"))
            (Path(result.path) / entry["path"]).write_text("tampered", encoding="utf-8")
            with self.assertRaises(BackupError):
                LocalBackup(root / "backups").verify(result.path)

    def test_attestation_must_not_contain_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sources = self.sources(root / "sources")
            with self.assertRaises(BackupError):
                LocalBackup(root / "backups").create(sources, encryption_attestation="token=glpat-123456789012345678")
            with self.assertRaises(BackupError):
                LocalBackup(root / "backups").create(sources, encryption_attestation="https://user:password@example.invalid/nas")

    def test_verify_rejects_symlinked_backup_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sources = self.sources(root / "sources")
            result = LocalBackup(root / "backups").create(sources, encryption_attestation="encrypted disk")
            outside = root / "outside.txt"
            outside.write_text("outside", encoding="utf-8")
            link = Path(result.path) / "agent_worktrees_root" / "escape.txt"
            link.symlink_to(outside)
            with self.assertRaises(BackupError):
                LocalBackup(root / "backups").verify(result.path)

    def test_cli_backup_requires_confirmation_and_restore_is_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sources = self.sources(root / "sources")
            source_manifest = root / "sources.json"
            source_manifest.write_text(json.dumps({key: str(value) for key, value in sources.items()}), encoding="utf-8")
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["backup", str(source_manifest), "--output", str(root / "backups"), "--attestation", "encrypted disk", "--json"]), 0)
            self.assertFalse((root / "backups").exists())
            preview = json.loads(output.getvalue())
            self.assertFalse(preview["confirmed"])
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["backup", str(source_manifest), "--output", str(root / "backups"), "--attestation", "encrypted disk", "--yes", "--json"]), 0)
            backup_result = json.loads(output.getvalue())
            backup_path = backup_result["path"]
            destinations = {component: str(root / "restored" / component) for component in COMPONENTS}
            destinations["catalog"] = str(root / "restored" / "catalog.yaml")
            destination_file = root / "destinations.json"
            destination_file.write_text(json.dumps(destinations), encoding="utf-8")
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["restore", backup_path, str(destination_file), "--yes", "--json"]), 0)
            restore_result = json.loads(output.getvalue())
        self.assertEqual(set(restore_result["restored_components"]), set(COMPONENTS))


if __name__ == "__main__":
    unittest.main()
