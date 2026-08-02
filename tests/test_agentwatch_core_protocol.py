from __future__ import annotations

import unittest
from unittest import mock

import agentwatch_core


class AgentWatchSourceProtocolTests(unittest.TestCase):
    def test_publish_preserves_claude_as_an_allowed_source(self) -> None:
        api = agentwatch_core.AgentWatchApi("https://example.test/api/v1")

        with mock.patch.object(
            api, "_post", return_value=(202, {"ok": True, "event_id": "event-1"})
        ) as post:
            response = api.publish(
                "computer-token",
                event_id="event-1",
                source="Claude",
                title="Claude Code complete",
                body="The Claude Code turn is complete.",
            )

        self.assertTrue(response["ok"])
        path, payload = post.call_args.args
        self.assertEqual("/publish", path)
        self.assertEqual("claude", payload["source"])
        self.assertEqual("computer-token", post.call_args.kwargs["token"])


if __name__ == "__main__":
    unittest.main()
