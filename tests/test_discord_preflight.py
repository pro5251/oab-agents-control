import unittest

from oab_control.discord_preflight import run_preflight


class FakeClient:
    def get_channel(self, channel):
        return {"id": channel}

    def read_messages(self, channel):
        return []

    def trigger_typing(self, channel):
        return None


class DiscordPreflightTests(unittest.TestCase):
    def test_catalog_topology_and_online_capabilities_are_reported(self):
        catalog = {"agents": {
            "leader": {"role": "leader", "discord": {"bot_user_id": "1", "trusted_bot_ids": ["2"], "entry_channel_id": "10"}},
            "reviewer": {"role": "reviewer", "discord": {"bot_user_id": "2", "trusted_bot_ids": ["1"], "work_channel_id": "20"}},
        }}
        result = run_preflight(catalog, {"leader": FakeClient(), "reviewer": FakeClient()})
        self.assertTrue(result["ready"])
        self.assertEqual(result["workers"]["reviewer"]["thread_access"], "deferred_to_dispatch")

    def test_missing_leader_send_capability_fails_closed(self):
        class NoTyping(FakeClient):
            def trigger_typing(self, channel):
                raise PermissionError()
        catalog = {"agents": {
            "leader": {"role": "leader", "discord": {"bot_user_id": "1", "trusted_bot_ids": ["2"], "entry_channel_id": "10"}},
            "reviewer": {"role": "reviewer", "discord": {"bot_user_id": "2", "trusted_bot_ids": ["1"], "work_channel_id": "20"}},
        }}
        result = run_preflight(catalog, {"leader": NoTyping(), "reviewer": FakeClient()})
        self.assertFalse(result["ready"])
