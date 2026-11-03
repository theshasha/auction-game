"""文本协议只解析完整提交，正文与审计原文分别保真。"""

import pytest

from auction_game.agent.text_output import LetterNames, OutputFormatError, output_requirement, parse_text_output
from auction_game.game.settings import validate_submission
from tests.support.factories import TEST_GAME, text_reply, task_entries


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_multiline_body_preserves_markdown_quotes_and_backslashes(newline):
    strategy = '# 策略\n\n- "保留引号"\n- 字面 \\n 与 C:\\new'
    raw = text_reply({"strategy": strategy, "bid": 2, "super_bid": None}).replace("\n", newline)
    result = parse_text_output("plan", " \n" + raw + "\n ")
    assert result == {"strategy": strategy.replace("\n", newline), "bid": 2, "super_bid": None, "letters": []}


def test_public_speech_and_private_strategy_are_separate():
    result = parse_text_output("speech", text_reply({"strategy": "私有\n\n策略", "speech": "公开\n发言"}))
    assert result == {"strategy": "私有\n\n策略", "speech": "公开\n发言", "letters": []}


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
    from auction_game.agent.tasks import TaskRunner
    from tests.support.factories import make_ai_obs, make_player
    from tests.support.wire import Harness, reply

    invalid = GOOD.replace('"bid": 2', '"bid": ' + "[" * 10000 + "0" + "]" * 10000)
    harness = Harness("chat", [reply(text=invalid), reply()])
    player = make_player(TaskRunner(harness.factory), tmp_path)
    assert player.bid(make_ai_obs(), {})[0] == (2, None)
    assert len(harness.requests) == 2
    entry = task_entries(player.log.path)[-1]
    texts = [part["content"] for message in entry["messages"] for part in message["parts"]
             if part["part_kind"] == "text"]
    assert texts[0] == invalid


LETTERS = LetterNames("甲", ("乙", "丙", "丁"))
SPEECH = "<strategy>\n策略\n</strategy>\n<speech>\n发言\n</speech>"
ORDER = "必须先依次提交 strategy 与 decision 两块，标签各占一行且各出现一次，私信块放在最后。"
PAIR = "每个私信块以 <名字> 开始、以同一名字的 </名字> 结束，标签各占一行。"
OUTSIDE = "协议块外只允许空白。私信块的标签必须是其他玩家的名字：乙、丙、丁。"


def letter(name, body="私信"):
    return f"\n<{name}>\n{body}\n</{name}>"


@pytest.mark.parametrize("task,base,fields", [
    ("bid", GOOD, {"bid": 2, "super_bid": None}),
    ("speech", SPEECH, {"speech": "发言"}),
])
@pytest.mark.parametrize("blocks,expected", [
    ("", []),
    (letter("乙", "第一行\n\n第二行"), [{"to": "乙", "text": "第一行\n\n第二行"}]),
    (letter("丙", "给丙") + "\n" + letter("乙", "给乙"), [{"to": "丙", "text": "给丙"}, {"to": "乙", "text": "给乙"}]),
    ("\n \t<乙>\t \n私信\n  </乙> \n", [{"to": "乙", "text": "私信"}]),
], ids=["none", "multiline", "two-in-order", "padded-tags"])
def test_letters_follow_fixed_blocks_in_order(task, base, fields, blocks, expected):
    assert parse_text_output(task, base + blocks, LETTERS) == {"strategy": "策略", **fields, "letters": expected}


@pytest.mark.parametrize("raw,message", [
    (GOOD + letter("戊"), OUTSIDE),
    (GOOD + letter("乙") + "\n尾随正文", OUTSIDE),
    (GOOD + "\n<戊>\n私信\n</乙>", PAIR),
    (GOOD + "\n<乙>\n私信\n</戊>", PAIR),
    (GOOD + "\n<乙>\n私信\n</丙>", PAIR),
    (GOOD + "\n<乙>\n私信", PAIR),
    (letter("乙").lstrip() + "\n" + GOOD, ORDER),
    (GOOD.replace("</strategy>\n", "</strategy>" + letter("乙") + "\n"), ORDER),
    (GOOD + letter("乙", "<speech>\n嵌套\n</speech>"), ORDER),
    (GOOD + letter("甲"), "私信不能写给自己。"),
    (GOOD + letter("乙") + letter("丙") + letter("丁"), "最多写 2 封私信。"),
    (GOOD + letter("乙") + letter("乙"), "每名玩家最多写 1 封私信，写给乙的有 2 封。"),
    (GOOD + letter("乙", " \n\t"), "写给乙的私信正文不能为空。"),
], ids=["unknown-name", "trailing-text", "wrong-start", "wrong-end", "mismatched", "unclosed", "before-strategy",
        "between-blocks", "fixed-tag-inside", "to-self", "three", "repeated", "blank"])
def test_letter_format_errors(raw, message):
    with pytest.raises(OutputFormatError) as exc:
        parse_text_output("bid", raw, LETTERS)
    assert str(exc.value) == message


def test_speech_order_error_names_speech_block():
    with pytest.raises(OutputFormatError) as exc:
        parse_text_output("speech", letter("乙").lstrip() + "\n" + SPEECH, LETTERS)
    assert str(exc.value) == "必须先依次提交 strategy 与 speech 两块，标签各占一行且各出现一次，私信块放在最后。"


def test_without_letter_names_name_tags_are_plain_text():
    with pytest.raises(OutputFormatError) as exc:
        parse_text_output("speech", SPEECH + letter("乙"))
    assert str(exc.value) == "协议块外只允许空白，请直接提交两块正文。"
    speech = parse_text_output("speech", SPEECH.replace("发言", "<乙>\n发言\n</乙>"))["speech"]
    assert speech == "<乙>\n发言\n</乙>"


@pytest.mark.parametrize("task", ["bid", "plan", "speech"])
def test_letter_format_lists_recipients_and_ends_with_letter_block(task):
    text = output_requirement(task, LETTERS)
    assert ("私信块可以不写，最多 2 块。标签是收件人的名字，可以写给乙、丙、丁，每人最多 1 封，每封 200 字左右。"
            "收件人下一次行动时才收到，影响不了对方这一次的出价或发言。") in text
    assert text.endswith("\n<收件人名字>\n只有收件人能看到的私信。\n</收件人名字>")
    assert "收件人" not in output_requirement(task)
