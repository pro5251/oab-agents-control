import unittest
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]


class TaskfileTests(unittest.TestCase):
    def test_exposes_safe_operator_commands(self):
        document = yaml.safe_load((REPO_ROOT / "Taskfile.yml").read_text(encoding="utf-8"))
        tasks = document["tasks"]

        self.assertTrue({
            "pods",
            "images",
            "logs",
            "logs:follow",
            "auth",
            "restart",
            "restart:all",
            "preflight",
            "deploy:preview",
            "deploy",
        }.issubset(tasks))
        self.assertIn("$OPENAB_AGENT_AUTH_COMMAND", "\n".join(tasks["auth"]["cmds"]))
        self.assertIn("CONFIRM=yes", "\n".join(tasks["deploy"]["cmds"]))
        self.assertIn("--yes", "\n".join(tasks["deploy"]["cmds"]))
        self.assertIn("kubeconfig:check", tasks)
        for task_name in ("pods", "images", "logs", "logs:follow", "logs:previous", "auth", "restart", "restart:all"):
            self.assertIn("kubeconfig:check", tasks[task_name]["deps"])


if __name__ == "__main__":
    unittest.main()
