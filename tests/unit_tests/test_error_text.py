# coding: utf-8
"""错误文本归一化单测（空异常消息不能变成空 chat.error）。"""

from __future__ import annotations

import unittest

from jiuwenswarm.common.error_text import (
    chat_error_payload,
    exception_text,
    leader_error_text,
    non_empty_text,
)


class ErrorTextTest(unittest.TestCase):
    def test_non_empty_text_skips_blank_and_non_string(self) -> None:
        self.assertEqual(
            non_empty_text("", "   ", None, 1, " 真实错误 "),
            "真实错误",
        )
        self.assertEqual(non_empty_text("", None, fallback="兜底"), "兜底")

    def test_exception_text_keeps_empty_timeout_type(self) -> None:
        self.assertEqual(
            exception_text(TimeoutError(), fallback="db init timeout"),
            "TimeoutError: db init timeout",
        )
        self.assertEqual(
            exception_text(RuntimeError("database is locked")),
            "database is locked",
        )

    def test_leader_error_text_never_empty(self) -> None:
        self.assertEqual(leader_error_text("模型调用失败"), "模型调用失败")
        self.assertTrue(leader_error_text("").strip())
        self.assertTrue(leader_error_text(None).strip())

    def test_chat_error_payload_keeps_original_keys_and_non_empty_error(self) -> None:
        payload = chat_error_payload(TimeoutError(), fallback="db init timeout")
        # 评审契约：不扩展 chat.error 键集合，只保证 error 非空。
        self.assertEqual(set(payload), {"event_type", "error"})
        self.assertEqual(payload["event_type"], "chat.error")
        self.assertTrue(payload["error"].strip())


if __name__ == "__main__":
    unittest.main()