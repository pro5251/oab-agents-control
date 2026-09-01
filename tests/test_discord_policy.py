from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from oab_control.catalog import validate_catalog
from oab_control.discord_policy import DiscordMessage, DiscordPolicyError, dispatch_channel, evaluate_message
from test_catalog import catalog


class DiscordPolicyTests(unittest.TestCase):
    def normalized(self) -> dict:
        with tempfile.TemporaryDirectory() as directory:
            value, diagnostics = validate_catalog(catalog(Path(directory)))
        self.assertEqual(diagnostics, [])
        assert value is not None
        return value

    def test_leader_accepts_allowlisted_human_only_with_mention(self) -> None:
        value = self.normalized()
        leader = value["agents"]["leader"]["discord"]
        accepted = evaluate_message(
            value,
            agent_id="leader",
            message=DiscordMessage(
                channel_id=leader["entry_channel_id"],
                author_id=leader["allowed_users"][0],
                author_is_bot=False,
                mentioned_bot_ids=(leader["bot_user_id"],),
            ),
        )
        self.assertTrue(accepted.allowed)
        self.assertEqual(accepted.audit_event["author_id"], leader["allowed_users"][0])

        denied = evaluate_message(
            value,
            agent_id="leader",
            message=DiscordMessage(
                channel_id=leader["entry_channel_id"],
                author_id=leader["allowed_users"][0],
                author_is_bot=False,
            ),
        )
        self.assertFalse(denied.allowed)
        self.assertIn("mention", denied.reason)

    def test_worker_rejects_humans_and_untrusted_or_unmentioned_bots(self) -> None:
        value = self.normalized()
        worker = value["agents"]["developer"]["discord"]
        leader = value["agents"]["leader"]["discord"]
        human = evaluate_message(
            value,
            agent_id="developer",
            message=DiscordMessage(
                channel_id=worker["work_channel_id"],
                author_id=leader["allowed_users"][0],
                author_is_bot=False,
                mentioned_bot_ids=(worker["bot_user_id"],),
            ),
        )
        self.assertFalse(human.allowed)
        untrusted = evaluate_message(
            value,
            agent_id="developer",
            message=DiscordMessage(
                channel_id=worker["work_channel_id"],
                author_id=value["agents"]["reviewer"]["discord"]["bot_user_id"],
                author_is_bot=True,
                mentioned_bot_ids=(worker["bot_user_id"],),
            ),
        )
        self.assertFalse(untrusted.allowed)
        unmentioned = evaluate_message(
            value,
            agent_id="developer",
            message=DiscordMessage(
                channel_id=worker["work_channel_id"],
                author_id=leader["bot_user_id"],
                author_is_bot=True,
            ),
        )
        self.assertFalse(unmentioned.allowed)

    def test_trusted_leader_bot_and_private_dispatch_target(self) -> None:
        value = self.normalized()
        worker = value["agents"]["developer"]["discord"]
        leader = value["agents"]["leader"]["discord"]
        accepted = evaluate_message(
            value,
            agent_id="developer",
            message=DiscordMessage(
                channel_id=worker["work_channel_id"],
                author_id=leader["bot_user_id"],
                author_is_bot=True,
                mentioned_bot_ids=(worker["bot_user_id"],),
            ),
        )
        self.assertTrue(accepted.allowed)
        envelope = {
            "task_id": "task-001",
            "repo": "/srv/repositories/team-a/service-x",
            "checkout_subpath": "team-a/service-x",
            "worktree_path": "/srv/worktrees/developer",
            "branch": "task/task-001",
            "base_branch": "origin/develop",
            "delivery_owner": "developer",
            "deadline": "2026-09-01T00:00:00Z",
            "reply_to": "discord:developer-private",
        }
        self.assertEqual(dispatch_channel(value, agent_id="developer", task_envelope=envelope), worker["work_channel_id"])
        with self.assertRaises(DiscordPolicyError):
            dispatch_channel(value, agent_id="leader", task_envelope=envelope)

    def test_wrong_channel_is_denied_and_denial_is_audit_event(self) -> None:
        value = self.normalized()
        worker = value["agents"]["developer"]["discord"]
        decision = evaluate_message(
            value,
            agent_id="developer",
            message=DiscordMessage(
                channel_id=value["agents"]["researcher"]["discord"]["work_channel_id"],
                author_id=value["agents"]["leader"]["discord"]["bot_user_id"],
                author_is_bot=True,
                mentioned_bot_ids=(worker["bot_user_id"],),
            ),
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.audit_event["event"], "discord_message_admission")
        self.assertFalse(decision.audit_event["allowed"])


if __name__ == "__main__":
    unittest.main()
