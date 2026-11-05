"""文本协议只解析完整提交，正文与审计原文分别保真。"""

import pytest

from auction_game.agent.text_output import (BOARD, DECISION, SPEECH, Blocks, LetterNames, OutputFormatError, TeamBoard,
                                            output_requirement, parse_text_output)
from auction_game.game.settings import validate_submission
from tests.support.factories import TEST_GAME, text_reply, task_entries


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_multiline_body_preserves_markdown_quotes_and_backslashes(newline):
    strategy = '# 策略\n\n- "保留引号"\n- 字面 \\n 与 C:\\new'
    raw = text_reply({"strategy": strategy, "bid": 2, "super_bid": None}).replace("\n", newline)
    result = parse_text_output(DECISION, " \n" + raw + "\n ")
    assert result == Blocks(strategy.replace("\n", newline), {"bid": 2, "super_bid": None}, [])


def test_public_speech_and_private_strategy_are_separate():
    result = parse_text_output(SPEECH, text_reply({"strategy": "私有\n\n策略", "speech": "公开\n发言"}))
    assert result == Blocks("私有\n\n策略", "公开\n发言", [])


def test_plain_body():
    body = "笔记\n\n保留字面 \\n"
    assert parse_text_output(None, " \n" + body + "\n") == body
    with pytest.raises(OutputFormatError):
        parse_text_output(None, " \n\t")


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
        parse_text_output(DECISION, raw)


@pytest.mark.parametrize("field,value", [("bid", True), ("bid", 2.0), ("bid", "2"),
                                         ("super_bid", False), ("super_bid", 2.0), ("super_bid", "2"),
                                         ("super_bid", "null")])
def test_fields_keep_strict_types(field, value):
    decision = parse_text_output(DECISION, text_reply({"strategy": "策略", "bid": 2, "super_bid": None, field: value})).body
    assert validate_submission(decision["bid"], decision["super_bid"], 6, TEST_GAME)


def test_deeply_nested_decision_gets_correction_and_keeps_audit(tmp_path):
    from auction_game.agent.tasks import TaskRunner
    from tests.support.factories import make_ai_obs, make_player
    from tests.support.wire import Harness, reply

    invalid = GOOD.replace('"bid": 2', '"bid": ' + "[" * 10000 + "0" + "]" * 10000)
    harness = Harness("chat", [reply(text=invalid), reply()])
    player = make_player(TaskRunner(harness.factory), tmp_path)
    assert player.bid(make_ai_obs(), {}, "第 1 局第 2 天出价").submission == (2, None)
    assert len(harness.requests) == 2
    entry = task_entries(player.log.path)[-1]
    texts = [part["content"] for message in entry["messages"] for part in message["parts"]
             if part["part_kind"] == "text"]
    assert texts[0] == invalid


LETTERS = LetterNames("甲", ("乙", "丙", "丁"))
SPEECH_TEXT = "<strategy>\n策略\n</strategy>\n<speech>\n发言\n</speech>"
ORDER = "必须先依次提交 strategy 与 decision 两块，标签各占一行且各出现一次，私信块放在最后。"
PAIR = "每个私信块以 <名字> 开始、以同一名字的 </名字> 结束，标签各占一行。"
OUTSIDE = "协议块外只允许空白。私信块的标签必须是其他玩家的名字：乙、丙、丁。"


def letter(name, body="私信"):
    return f"\n<{name}>\n{body}\n</{name}>"


@pytest.mark.parametrize("second,base,body", [
    (DECISION, GOOD, {"bid": 2, "super_bid": None}),
    (SPEECH, SPEECH_TEXT, "发言"),
])
@pytest.mark.parametrize("blocks,expected", [
    ("", []),
    (letter("乙", "第一行\n\n第二行"), [{"to": "乙", "text": "第一行\n\n第二行"}]),
    (letter("丙", "给丙") + "\n" + letter("乙", "给乙"), [{"to": "丙", "text": "给丙"}, {"to": "乙", "text": "给乙"}]),
    ("\n \t<乙>\t \n私信\n  </乙> \n", [{"to": "乙", "text": "私信"}]),
], ids=["none", "multiline", "two-in-order", "padded-tags"])
def test_letters_follow_fixed_blocks_in_order(second, base, body, blocks, expected):
    assert parse_text_output(second, base + blocks, LETTERS) == Blocks("策略", body, expected)


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
        parse_text_output(DECISION, raw, LETTERS)
    assert str(exc.value) == message


def test_speech_order_error_names_speech_block():
    with pytest.raises(OutputFormatError) as exc:
        parse_text_output(SPEECH, letter("乙").lstrip() + "\n" + SPEECH_TEXT, LETTERS)
    assert str(exc.value) == "必须先依次提交 strategy 与 speech 两块，标签各占一行且各出现一次，私信块放在最后。"


def test_without_letter_names_name_tags_are_plain_text():
    with pytest.raises(OutputFormatError) as exc:
        parse_text_output(SPEECH, SPEECH_TEXT + letter("乙"))
    assert str(exc.value) == "协议块外只允许空白，请直接提交两块正文。"
    speech = parse_text_output(SPEECH, SPEECH_TEXT.replace("发言", "<乙>\n发言\n</乙>")).body
    assert speech == "<乙>\n发言\n</乙>"


@pytest.mark.parametrize("second", [DECISION, SPEECH])
def test_letter_format_lists_recipients_and_ends_with_letter_block(second):
    text = output_requirement(second, LETTERS)
    assert ("私信块可以不写，最多 2 块。标签是收件人的名字，可以写给乙、丙、丁，每人最多 1 封，每封 200 字左右。\n"
            in text)
    assert text.endswith("\n<收件人名字>\n只有收件人能看到的私信。\n</收件人名字>")
    assert "收件人" not in output_requirement(second)


BOARD_LETTER = letter(BOARD, "第一行\n\n第二行")
TEAM_ORDER = "必须先依次提交 strategy 与 decision 两块，标签各占一行且各出现一次，队内信件区的信放在最后。"
TEAM_PAIR = "队内信件区的信以 <队内信件区> 开始、以 </队内信件区> 结束，标签各占一行。"
TEAM_OUTSIDE = "协议块外只允许空白。给队友的信只能写在 <队内信件区> 块里。"


@pytest.mark.parametrize("second,base,body", [
    (DECISION, GOOD, {"bid": 2, "super_bid": None}),
    (SPEECH, SPEECH_TEXT, "发言"),
])
def test_team_letter_goes_to_the_board(second, base, body):
    assert parse_text_output(second, base, TeamBoard()) == Blocks("策略", body, [])
    written = [{"to": BOARD, "text": "第一行\n\n第二行"}]
    assert parse_text_output(second, base + BOARD_LETTER, TeamBoard()) == Blocks("策略", body, written)


@pytest.mark.parametrize("raw,message", [
    (GOOD + letter("乙"), TEAM_OUTSIDE),
    (GOOD + BOARD_LETTER + "\n尾随正文", TEAM_OUTSIDE),
    (GOOD + f"\n<{BOARD}>\n信", TEAM_PAIR),
    (GOOD + f"\n<{BOARD}>\n信\n<{BOARD}>", TEAM_PAIR),
    (BOARD_LETTER.lstrip() + "\n" + GOOD, TEAM_ORDER),
    (GOOD + letter(BOARD, "<speech>\n嵌套\n</speech>"), TEAM_ORDER),
    (GOOD + BOARD_LETTER + BOARD_LETTER, "最多写 1 封队内信件区的信。"),
    (GOOD + letter(BOARD, " \n\t"), "队内信件区的信正文不能为空。"),
], ids=["name-tag", "trailing-text", "unclosed", "mismatched", "before-strategy", "fixed-tag-inside", "two", "blank"])
def test_team_letter_format_errors(raw, message):
    with pytest.raises(OutputFormatError) as exc:
        parse_text_output(DECISION, raw, TeamBoard())
    assert str(exc.value) == message


DISCUSSION = f"<strategy>\n策略\n</strategy>{BOARD_LETTER}"


def test_discussion_returns_the_letter_as_body():
    assert parse_text_output(BOARD, DISCUSSION) == Blocks("策略", "第一行\n\n第二行", [])


@pytest.mark.parametrize("raw", ["<strategy>\n策略\n</strategy>", DISCUSSION + BOARD_LETTER], ids=["missing", "two"])
def test_discussion_needs_exactly_one_letter(raw):
    with pytest.raises(OutputFormatError) as exc:
        parse_text_output(BOARD, raw)
    assert str(exc.value) == "必须依次提交 strategy 与 队内信件区 两块，标签各占一行且各出现一次。"


def test_discussion_letter_cannot_be_blank():
    with pytest.raises(OutputFormatError) as exc:
        parse_text_output(BOARD, f"<strategy>\n策略\n</strategy>{letter(BOARD, ' ')}")
    assert str(exc.value) == "strategy 与 队内信件区 都必须有非空正文。"


@pytest.mark.parametrize("second", [DECISION, SPEECH])
def test_team_format_shows_the_board_block(second):
    text = output_requirement(second, TeamBoard())
    assert "队内信件区的信可以不写，最多 1 封，约 300 字。\n" in text
    assert text.endswith("\n<队内信件区>\n只有队友能看到的信。\n</队内信件区>")
    assert "私信" not in text and "收件人" not in text


def test_discussion_format_has_strategy_and_letter_blocks():
    assert output_requirement(BOARD).endswith(
        "\n<strategy>\n仅自己可见的讨论策略。\n</strategy>\n<队内信件区>\n只有队友能看到的信。\n</队内信件区>")


def test_team_name_tag_inside_a_letter_stays_text():
    raw = GOOD + letter(BOARD, "<乙>\n信\n</乙>")
    assert parse_text_output(DECISION, raw, TeamBoard()).letters == [{"to": BOARD, "text": "<乙>\n信\n</乙>"}]
