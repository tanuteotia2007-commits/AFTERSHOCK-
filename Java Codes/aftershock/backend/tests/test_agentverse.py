import unittest

from aftershock.agents.agentverse import (
    AgentverseAdapter,
    AgentverseConfigurationError,
)


class AgentverseAdapterTests(unittest.TestCase):
    def test_missing_credentials_are_reported_by_name(self):
        with self.assertRaisesRegex(
            AgentverseConfigurationError,
            "AGENTVERSE_AGENT_ADDRESS, AGENTVERSE_API_KEY",
        ):
            AgentverseAdapter(address="", api_key="")

    def test_duplicate_sends_twice_and_labels_the_real_agent(self):
        sent = []

        def sender(address, message, timeout, malformed):
            sent.append((address, message, timeout, malformed))
            return f"reply-{len(sent)}"

        result = AgentverseAdapter(
            address="agent1example",
            api_key="test-key",
            sender=sender,
        ).run("Hello", fault="duplicated_message")

        self.assertEqual(len(sent), 2)
        self.assertEqual([reply for reply in result["outcome"]["responses"]], ["reply-1", "reply-2"])
        self.assertEqual(
            result["execution_type"],
            "REAL AGENT + SIMULATED MESSAGE FAULT",
        )
        self.assertTrue(result["passed"])

    def test_dropped_message_is_not_sent(self):
        def sender(*args):
            self.fail("a dropped message must not be sent")

        result = AgentverseAdapter(
            address="agent1example",
            api_key="test-key",
            sender=sender,
        ).run("Hello", fault="dropped_message")

        self.assertFalse(result["passed"])
        self.assertEqual(result["violations"][0]["code"], "MESSAGE_DROPPED")
        self.assertEqual(result["events"][1]["kind"], "fault_injected")
        self.assertEqual(result["events"][2]["delivery"], "not_sent")

    def test_malformed_payload_and_delayed_reply_are_recorded(self):
        sent = []

        def sender(address, message, timeout, malformed):
            sent.append(malformed)
            return "reply"

        adapter = AgentverseAdapter(
            address="agent1example",
            api_key="test-key",
            sender=sender,
        )
        malformed = adapter.run("Hello", fault="malformed_payload")
        delayed = adapter.run("Hello", fault="delayed_reply", delay_ms=0)

        self.assertTrue(sent[0])
        self.assertFalse(sent[1])
        self.assertTrue(malformed["events"][2]["malformed_payload"])
        self.assertEqual(
            [event["kind"] for event in delayed["events"][-3:]],
            ["reply_delay_started", "reply_delay_completed", "agent_result"],
        )


if __name__ == "__main__":
    unittest.main()
