"""Regression tests for the Gradio chat-history boundary."""

import unittest

from app import _append_assistant, _history_pairs, _message_text


def _user(content) -> dict:
    return {"role": "user", "content": content}


class TestChatHistoryFormat(unittest.TestCase):
    def test_messages_roundtrip_to_agent_pairs(self):
        history = _append_assistant([_user("First question")], "First answer")
        history = _append_assistant([*history, _user("Second question")], "Second answer")

        self.assertEqual(
            [
                {"role": "user", "content": "First question"},
                {"role": "assistant", "content": "First answer"},
                {"role": "user", "content": "Second question"},
                {"role": "assistant", "content": "Second answer"},
            ],
            history,
        )
        self.assertEqual(
            [
                ("First question", "First answer"),
                ("Second question", "Second answer"),
            ],
            _history_pairs(history),
        )
        postprocessed_history = [
            {
                "role": message["role"],
                "content": [{"text": message["content"], "type": "text"}],
            }
            for message in history
        ]
        self.assertEqual(
            [
                ("First question", "First answer"),
                ("Second question", "Second answer"),
            ],
            _history_pairs(postprocessed_history),
        )

    def test_message_text_handles_both_content_shapes(self):
        self.assertEqual("plain", _message_text("plain"))
        self.assertEqual(
            "rich", _message_text([{"type": "text", "text": "rich"}])
        )
        self.assertEqual("", _message_text(None))

    def test_unanswered_user_message_yields_open_pair(self):
        """Stage 2 slices history[:-1]; an unanswered trailing user message
        must still roundtrip when a blocked turn leaves one behind."""
        history = [_user("First"), {"role": "assistant", "content": "A1"}, _user("Pending")]
        self.assertEqual([("First", "A1"), ("Pending", "")], _history_pairs(history))


if __name__ == "__main__":
    unittest.main(verbosity=2)
