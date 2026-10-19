"""文本协议只解析完整提交，正文与审计原文分别保真。"""

import pytest

from auction_game.ai.text_output import OutputFormatError, parse_text_output
from auction_game.game.game_rules import validate_submission
from tests.support.factories import TEST_GAME, text_reply, task_entries


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_multiline_body_preserves_markdown_quotes_and_backslashes(newline):
    strategy = '# 策略\n\n- "保留引号"\n- 字面 \\n 与 C:\\new'
    raw = text_reply({"strategy": strategy, "bid": 2, "super_bid": None}).replace("\n", newline)
    result = parse_text_output("plan", " \n" + raw + "\n ")
    assert result == {"strategy": strategy.replace("\n", newline), "bid": 2, "super_bid": None}


def test_public_speech_and_private_strategy_are_separate():
    result = parse_text_output("speech", text_reply({"strategy": "私有\n\n策略", "speech": "公开\n发言"}))
    assert result == {"strategy": "私有\n\n策略", "speech": "公开\n发言"}


@pytest.mark.parametrize("task", ["memory", "summary"])
def test_plain_body(task):
    body = "笔记\n\n保留字面 \\n"
    assert parse_text_output(task, " \n" + body + "\n") == ({"memory": body} if task == "memory" else body)
    with pytest.raises(OutputFormatError):
        parse_text_output(task, " \n\t")


GOOD = '<strategy>\n策略\n</strategy>\n<decision>\n{"bid": 2, "super_bid": null}\n</decision>'


@pytest.mark.parametrize("raw", [
    GOOD.replace("</decision>", ""),
    GOOD + "\n" + GOOD,
    GOOD.replace("策略", "<speech>\n嵌套\n</speech>"),
    GOOD.replace("策略", " "),
    GOOD.replace('<strategy>\n策略\n</strategy>', ''),
    GOOD.replace("</strategy>\n<decision>", "</strategy>\n额外正文\n<decision>"),
    "额外正文\n" + GOOD,
    GOOD + "\n尾随正文",
    "```text\n" + GOOD + "\n```",
    GOOD.replace('{"bid": 2, "super_bid": null}', '```json\n{"bid": 2, "super_bid": null}\n```'),
    GOOD.replace('{"bid": 2, "super_bid": null}', '{"bid": 2, "bid": 3, "super_bid": null}'),
    GOOD.replace('{"bid": 2, "super_bid": null}', '{"bid": 2, "super_bid": null} {}'),
    GOOD.replace('{"bid": 2, "super_bid": null}', '{"bid": 2}'),
    GOOD.replace('{"bid": 2, "super_bid": null}', '{"bid": 2, "super_bid": null, "extra": 1}'),
    GOOD.replace('{"bid": 2, "super_bid": null}', '[]'),
    '<decision>\n{"bid": 2, "super_bid": null}\n</decision>\n<strategy>\n策略\n</strategy>',
])
def test_rejects_ambiguous_or_incomplete_protocol(raw):
    with pytest.raises(OutputFormatError):
        parse_text_output("bid", raw)


@pytest.mark.parametrize("field,value", [("bid", True), ("bid", 2.0), ("bid", "2"),
                                         ("super_bid", False), ("super_bid", 2.0), ("super_bid", "2"),
                                         ("super_bid", "null")])
def test_fields_keep_strict_types(field, value):
    decision = parse_text_output("bid", text_reply({"strategy": "策略", "bid": 2, "super_bid": None, field: value}))
    assert validate_submission(decision["bid"], decision["super_bid"], 6, TEST_GAME)


def test_deeply_nested_decision_gets_correction_and_keeps_audit(tmp_path):
    from auction_game.ai.agent_tasks import TaskRunner
    from tests.support.factories import make_ai_obs, make_player
    from tests.support.wire import Harness, reply

    invalid = GOOD.replace('"bid": 2', '"bid": ' + "[" * 10000 + "0" + "]" * 10000)
    harness = Harness("chat", [reply(text=invalid), reply()])
    player = make_player(TaskRunner(harness.factory), tmp_path)
    assert player.decide_bid(make_ai_obs()) == (2, None)
    assert len(harness.requests) == 2
    entry = task_entries(player.log_path)[-1]
    texts = [part["content"] for message in entry["messages"] for part in message["parts"]
             if part["part_kind"] == "text"]
    assert texts[0] == invalid
