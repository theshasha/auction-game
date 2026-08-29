"""llm_client 的纯函数测试：strip_think_blocks / 流式累积 / provider/protocol 分发，零网络。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from llm_client import (
    collect_chat_stream,
    collect_responses_stream,
    make_chat_fn,
    strip_think_blocks,
)


class TestStripThinkBlocks:
    def test_no_think_block_unchanged(self):
        text = '正常回复。\n```json\n{"bid": 42}\n```'
        assert strip_think_blocks(text) == text

    def test_leading_think_block_removed(self):
        text = '<think>让我想想……</think>\n最终回答。'
        assert strip_think_blocks(text) == '最终回答。'

    def test_multiple_think_blocks_removed(self):
        text = '<think>第一段</think>中间<think>第二段</think>结尾'
        assert strip_think_blocks(text) == '中间结尾'

    def test_multiline_think_block_removed(self):
        text = '<think>\n多行\n推理\n</think>\n\n正文保留'
        assert strip_think_blocks(text) == '正文保留'

    def test_unclosed_think_block_truncated(self):
        text = '前面内容<think>推理到一半被截断'
        assert strip_think_blocks(text) == '前面内容'

    def test_only_unclosed_think_block_becomes_empty(self):
        assert strip_think_blocks('<think>全是推理') == ''


class TestCollectChatStream:
    """chat completions 流式累积：只取 delta.content，忽略推理字段与空心跳块。"""

    @staticmethod
    def _chunk(content=None, reasoning=None):
        delta = SimpleNamespace(content=content, reasoning_content=reasoning)
        return SimpleNamespace(choices=[SimpleNamespace(delta=delta)])

    def test_accumulates_content_in_order(self):
        chunks = [self._chunk("第一句。"), self._chunk("第二句。"), self._chunk('{"bid": 3}')]
        assert collect_chat_stream(chunks) == '第一句。第二句。{"bid": 3}'

    def test_ignores_reasoning_content(self):
        chunks = [self._chunk(None, reasoning="推理ing"), self._chunk("正文")]
        assert collect_chat_stream(chunks) == "正文"

    def test_skips_empty_choices_heartbeat(self):
        chunks = [self._chunk("前"), SimpleNamespace(choices=[]), self._chunk("后")]
        assert collect_chat_stream(chunks) == "前后"

    def test_empty_stream_becomes_empty_string(self):
        assert collect_chat_stream([]) == ""


class TestCollectResponsesStream:
    """responses API 流式累积：只取 response.output_text.delta 事件。"""

    def test_accumulates_output_text_deltas(self):
        events = [
            SimpleNamespace(type="response.created"),
            SimpleNamespace(type="response.output_text.delta", delta="第一段。"),
            SimpleNamespace(type="response.reasoning_summary_text.delta", delta="推理"),
            SimpleNamespace(type="response.output_text.delta", delta="第二段。"),
            SimpleNamespace(type="response.completed"),
        ]
        assert collect_responses_stream(events) == "第一段。第二段。"

    def test_missing_delta_tolerated(self):
        events = [SimpleNamespace(type="response.output_text.delta")]
        assert collect_responses_stream(events) == ""

    def test_empty_stream_becomes_empty_string(self):
        assert collect_responses_stream([]) == ""


class TestMakeChatFnDispatch:
    def test_unknown_provider_raises(self):
        with pytest.raises(ValueError, match="未知 provider"):
            make_chat_fn(provider="openai", model="gpt-x")

    def test_hub_unknown_protocol_raises(self):
        with pytest.raises(ValueError, match="未知 protocol"):
            make_chat_fn(provider="hub", model="any", protocol="webrtc")
