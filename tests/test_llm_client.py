"""生产文本适配器的 SDK/SSE 边界，全部 HTTP 使用假密钥和 MockTransport。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json

import pytest

from llm_client import (
    IncompleteStream,
    strip_think_blocks,
    text_chat_fn,
    make_model_factory,
)
from pydantic_ai.exceptions import ModelAPIError
from test_pydantic_ai_contract import Harness, PROTOCOLS, reply


class TestStripThinkBlocks:
    @pytest.mark.parametrize("text, expected", [
        ('正常回复。\n```json\n{"bid": 42}\n```', '正常回复。\n```json\n{"bid": 42}\n```'),
        ('<think>让我想想……</think>\n最终回答。', '最终回答。'),
        ('<think>第一段</think>中间<think>第二段</think>结尾', '中间结尾'),
        ('<think>\n多行\n推理\n</think>\n\n正文保留', '正文保留'),
        ('前面内容<think>推理到一半被截断', '前面内容'),
        ('<think>全是推理', ''),
    ], ids=["plain", "leading", "multiple", "multiline", "unclosed", "only-unclosed"])
    def test_strip_think_blocks(self, text, expected):
        assert strip_think_blocks(text) == expected


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_text_adapter_history_budget_and_local_clients(protocol):
    h = Harness(protocol, [reply(tool=False, text="<think>private</think>正文"), reply(tool=False)])
    chat = text_chat_fn(h.factory)
    messages = [{"role": role, "content": content} for role, content in [
        ("system", "unique system"), ("user", "previous user"),
        ("assistant", "previous answer"), ("user", "current user")]]
    before = deepcopy(messages)
    assert chat(messages) == "正文"
    assert chat(messages) == "no tool"
    assert messages == before and len(h.requests) == 2
    assert h.closes == 2 and h.loops[0] is not h.loops[1]
    assert all(loop.is_closed() for loop in h.loops)
    for request, body in h.requests:
        encoded = json.dumps(body)
        for message in messages:
            assert encoded.count(message["content"]) == 1
        assert encoded.index("previous user") < encoded.index("previous answer") < encoded.index("current user")
        assert body["max_output_tokens" if protocol == "responses" else "max_tokens"] == 123
        assert request.extensions["timeout"]["read"] == 3
    assert all(stream.closed for stream in h.streams)


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("failure", ["missing", "limit", "stream", "connect", 400, 429, 500])
def test_text_adapter_never_retries_or_returns_partial(protocol, failure):
    import httpx2 as http
    item = (reply(tool=False, ending=failure) if failure in ("missing", "limit")
            else reply(tool=False, broken=True, ending="missing") if failure == "stream" else failure)
    h = Harness(protocol, [item])
    with pytest.raises((IncompleteStream, ModelAPIError, http.TransportError)):
        text_chat_fn(h.factory)([{"role": "user", "content": "current"}])
    assert len(h.requests) == 1 and all(stream.closed for stream in h.streams)
    assert h.closes == 1


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("fault", ["connect", "stream", 400])
def test_ai_player_owns_only_failure_retry(protocol, fault):
    from ai_player import AIPlayer
    from conftest import TEST_GAME
    item = reply(tool=False, broken=True, ending="missing") if fault == "stream" else fault
    h = Harness(protocol, [item, reply(tool=False)])
    player = AIPlayer("甲", "测试", "测试人设", [], ["甲", "乙"],
                      text_chat_fn(h.factory), game=TEST_GAME)
    assert player._guarded_call("today", "test") == "no tool"
    assert len(h.requests) == h.closes == 2
    assert h.requests[0][1] == h.requests[1][1]
    assert [m["role"] for m in player.messages] == ["system", "user", "assistant"]


@pytest.mark.parametrize("index", range(6))
def test_actual_player_configuration_drives_wire_request(index, monkeypatch):
    import llm_client
    from configuration import load_configuration
    config, players = load_configuration()
    player = players[index]
    channel = config["llm"]["channels"][player["provider"]]
    monkeypatch.setattr(llm_client, "load_dotenv", lambda path: None)
    monkeypatch.setenv(channel["api_key_env"], "fake-key")
    h = Harness(player["protocol"], [reply(tool=False)])
    factory = make_model_factory(provider=player["provider"], channel=channel, model=player["model"],
                                 protocol=player["protocol"], timeout=config["llm"]["timeout_seconds"],
                                 max_tokens=player["max_tokens"])
    assert factory.base_url == channel["base_url"]
    factory = replace(factory, base_url=h.factory.base_url, transport=h.factory.transport)
    assert text_chat_fn(factory)([{"role": "user", "content": "ping"}]) == "no tool"
    request, body = h.requests[0]
    assert body["model"] == player["model"]
    field = "max_output_tokens" if player["protocol"] == "responses" else "max_tokens"
    assert body[field] == player["max_tokens"]
    assert request.extensions["timeout"]["read"] == 900
    assert h.closes == 1
