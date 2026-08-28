"""提示词文案与表格渲染的测试：字数软要求、加赛预告、发言拼接空行、表格格式。"""

import prompts
from models import BidRecord, DayResult, MatchLog, StandingsEntry

NAMES = ["甲", "乙", "丙", "丁", "戊", "己"]


def make_day(day, bids, winner, winning_bid, voided, dividend, is_void=False):
    return DayResult(
        day=day,
        bids=[BidRecord(player=name, raw_bid=bid, bid=bid) for name, bid in zip(NAMES, bids)],
        winner=winner,
        winning_bid=winning_bid,
        voided_price_count=voided,
        dividend=dividend,
        is_void=is_void,
    )


def make_log(match_index=1, is_playoff=False, days=None, wins=None):
    return MatchLog(
        match_index=match_index,
        is_playoff=is_playoff,
        days=days if days is not None else [make_day(1, [5, 4, 3, 2, 1, 0], "甲", 5, 0, 2)],
        wins=wins if wins is not None else {name: 0 for name in NAMES},
    )


def make_standings(wins_history: dict[str, list[int]]):
    entries = [
        StandingsEntry(player=name, total_wins=sum(per_match), wins_per_match=list(per_match))
        for name, per_match in wins_history.items()
    ]
    entries.sort(key=lambda entry: (-entry.total_wins, entry.player))
    for index, entry in enumerate(entries):
        entry.rank = entries[index - 1].rank if index > 0 and entry.total_wins == entries[index - 1].total_wins else index + 1
    return entries


class TestSpeechWordLimits:
    """字数只是提示词软要求：开局/局末 300 字，感言/总结不限，规则文本不提字数。"""

    def test_opening_speech_asks_300_chars(self):
        assert "不超过 300 字" in prompts.opening_speech_request()

    def test_match_end_speech_asks_300_chars(self):
        text = prompts.match_end_speech_request(make_log(), make_standings({n: [0] for n in NAMES}), True)
        assert "不超过 300 字" in text

    def test_final_speech_and_summary_have_no_word_limit(self):
        standings = make_standings({n: [0] for n in NAMES})
        assert "字" not in prompts.final_speech_request(standings)
        assert "字" not in prompts.final_summary_request({"乙": "感言"})

    def test_rules_text_has_no_word_limit(self):
        assert "500 字" not in prompts.RULES_TEXT
        assert "300 字" not in prompts.RULES_TEXT


class TestPlayoffNotice:
    """加赛预告只在正赛打完（或加赛后）且第一名仍并列时出现。"""

    def test_no_notice_for_early_matches(self):
        text = prompts.match_end_speech_request(
            make_log(match_index=2), make_standings({n: [0, 0] for n in NAMES}), will_continue=True
        )
        assert "第一名出现并列" not in text
        assert "加赛局" not in text

    def test_notice_on_match5_when_tied(self):
        text = prompts.match_end_speech_request(
            make_log(match_index=5),
            make_standings({n: [0] * 5 for n in NAMES}),
            will_continue=True,
        )
        assert "第一名出现并列" in text
        assert "加赛局" in text

    def test_no_notice_on_match5_when_decided(self):
        text = prompts.match_end_speech_request(
            make_log(match_index=5),
            make_standings({n: [0] * 5 for n in NAMES}),
            will_continue=False,
        )
        assert "第一名出现并列" not in text

    def test_notice_after_playoff_when_still_tied(self):
        text = prompts.match_end_speech_request(
            make_log(match_index=6, is_playoff=True),
            make_standings({n: [0] * 6 for n in NAMES}),
            will_continue=True,
        )
        assert "第一名出现并列" in text


class TestSpeechJoinBlankLines:
    """他人发言拼接：两人之间空一行。"""

    def test_opening_plan_separates_speeches_with_blank_line(self):
        text = prompts.opening_plan_request({"甲": "发言A", "乙": "发言B"})
        assert "【甲】发言A\n\n【乙】发言B" in text

    def test_next_match_plan_separates_speeches_with_blank_line(self):
        text = prompts.next_match_plan_request({"甲": "发言A", "乙": "发言B"})
        assert "【甲】发言A\n\n【乙】发言B" in text

    def test_final_summary_separates_speeches_with_blank_line(self):
        text = prompts.final_summary_request({"甲": "感言A", "乙": "感言B"})
        assert "【甲】感言A\n\n【乙】感言B" in text


class TestRenderMatchLog:
    def test_renders_markdown_table_with_player_columns(self):
        log = make_log(days=[
            make_day(1, [5, 4, 3, 2, 1, 0], "甲", 5, 0, 2),
            make_day(2, [3, 3, 2, 2, 2, 0], "己", 0, 2, 3),
        ])
        text = prompts.render_match_log(log)
        lines = text.splitlines()

        assert lines[0] == "| 天 | 甲 | 乙 | 丙 | 丁 | 戊 | 己 | 结果 | 作废价位 | 分红 |"
        assert lines[2] == "| 1 | 5 | 4 | 3 | 2 | 1 | 0 | 甲 以 5 获胜 | 0 | 2 |"
        assert lines[3] == "| 2 | 3 | 3 | 2 | 2 | 2 | 0 | 己 以 0 获胜 | 2 | 3 |"
        assert "本局胜场：" in text

    def test_void_day_row(self):
        log = make_log(days=[make_day(1, [2, 2, 2, 1, 1, 1], None, None, 2, 0, is_void=True)])
        text = prompts.render_match_log(log)
        assert "| 1 | 2 | 2 | 2 | 1 | 1 | 1 | 流局（全额返还） | 2 | 0 |" in text

    def test_empty_log(self):
        log = make_log(days=[])
        assert "未进行任何拍卖" in prompts.render_match_log(log)


class TestRenderStandings:
    def test_renders_markdown_table_with_match_columns(self):
        standings = make_standings({"甲": [2, 1], "乙": [1, 1], "丙": [0, 0]})
        text = prompts.render_standings(standings)
        lines = text.splitlines()

        assert lines[0] == "| 排名 | 玩家 | 总胜场 | 第1局 | 第2局 |"
        assert "| 1 | 甲 | 3 | 2 | 1 |" in lines[2]
        assert "| 3 | 丙 | 0 | 0 | 0 |" in lines[4]

    def test_tied_players_share_rank_cell(self):
        standings = make_standings({"甲": [1], "乙": [1]})
        text = prompts.render_standings(standings)
        assert "| 1 | 甲 | 1 | 1 |" in text
        assert "| 1 | 乙 | 1 | 1 |" in text

    def test_no_match_columns_when_history_empty(self):
        standings = make_standings({"甲": [], "乙": []})
        text = prompts.render_standings(standings)
        assert text.splitlines()[0] == "| 排名 | 玩家 | 总胜场 |"
