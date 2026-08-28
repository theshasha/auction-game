"""llm_client 的纯函数测试：strip_think_blocks 与 provider/protocol 分发，零网络。"""

from __future__ import annotations

import pytest

from llm_client import make_chat_fn, strip_think_blocks


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


class TestMakeChatFnDispatch:
    def test_unknown_provider_raises(self):
        with pytest.raises(ValueError, match="未知 provider"):
            make_chat_fn(provider="openai", model="gpt-x")

    def test_hub_unknown_protocol_raises(self):
        with pytest.raises(ValueError, match="未知 protocol"):
            make_chat_fn(provider="hub", model="any", protocol="webrtc")
