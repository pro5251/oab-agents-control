import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]


class GenerateCatalogTests(unittest.TestCase):
    def test_renders_runtime_per_role(self):
        config = {
            "home": "/tmp/oab",
            "mirror_root": "/tmp/mirrors",
            "image": "fallback/image",
            "command": "fallback-command",
            "working_dir": "/home/agent",
            "runtimes": {
                "leader": {
                    "command": "codex-acp",
                    "args": [],
                    "image": "openab:codex",
                    "working_dir": "/home/node",
                },
                "researcher": {
                    "command": "copilot",
                    "args": ["--acp", "--stdio"],
                    "image": "openab:copilot",
                    "working_dir": "/home/node",
                },
                "developer": {
                    "command": "codex-acp",
                    "args": [],
                    "image": "openab:codex",
                    "working_dir": "/home/node",
                },
                "reviewer": {
                    "command": "copilot",
                    "args": ["--acp", "--stdio"],
                    "image": "openab:copilot",
                    "working_dir": "/home/node",
                },
            },
            "bots": {role: str(index) for index, role in enumerate(("leader", "researcher", "developer", "reviewer"), 1)},
            "channels": {role: str(index) for index, role in enumerate(("leader", "researcher", "developer", "reviewer"), 10)},
            "human_user_id": "99",
            "grants": [{"root": "/tmp/repos", "repo": "demo", "base_branch": "origin/main"}],
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / "catalog-input.json"
            input_path.write_text(json.dumps(config), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, "scripts/generate-catalog.py", "--input", str(input_path)],
                cwd=REPO_ROOT,
                check=True,
                capture_output=True,
                text=True,
            )

        catalog = yaml.safe_load(result.stdout)
        self.assertEqual(catalog["agents"]["leader"]["runtime"], {
            "command": "codex-acp",
            "args": [],
            "model": "model-leader",
            "image": "openab:codex",
            "working_dir": "/home/node",
        })
        self.assertEqual(catalog["agents"]["researcher"]["runtime"]["command"], "copilot")
        self.assertEqual(catalog["agents"]["researcher"]["runtime"]["args"], ["--acp", "--stdio"])
        self.assertEqual(catalog["agents"]["developer"]["runtime"]["image"], "openab:codex")
        self.assertEqual(catalog["agents"]["reviewer"]["runtime"]["image"], "openab:copilot")


if __name__ == "__main__":
    unittest.main()
