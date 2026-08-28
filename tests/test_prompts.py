"""提示词文案与表格渲染的测试：字数软要求、加赛预告、发言拼接空行、表格格式。"""

import prompts
from models import BidRecord, DayResult, MatchLog, Observation, StandingsEntry

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
        assert "字" not in prompts.final_speech_request(standings, make_log(match_index=5))
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


class TestPlanBidMerge:
    """规划请求与下一局第 1 天出价合并：附恒定首日状态与 JSON 出价格式。"""

    def test_opening_plan_asks_for_day1_bid(self):
        text = prompts.opening_plan_request({"乙": "发言"})
        assert "第 1 局第 1 天的出价" in text
        assert "你的金币 6，本局已获胜场 0，本局剩余待拍胜场 8" in text
        assert '{"bid":' in text
        assert "```json" not in text  # 示例无围栏，避免模型误以为必须包代码块

    def test_next_match_plan_asks_for_day1_bid(self):
        text = prompts.next_match_plan_request({"乙": "发言"})
        assert "下一局第 1 天的出价" in text
        assert "你的金币 6，本局已获胜场 0，本局剩余待拍胜场 8" in text
        assert '{"bid":' in text
        assert "```json" not in text


class TestBidRequest:
    """每日出价开场：首句「当前是」、昨日公布带「获胜」、余额行在公布之后且标注已含分红。"""

    @staticmethod
    def make_obs(day=1, today_history=None, total_wins=0):
        return Observation(
            my_name="甲",
            my_coins=7,
            my_match_wins=0,
            my_total_wins=total_wins,
            day=day,
            wins_remaining=2,
            max_days=5,
            today_history=today_history or [],
            standings=[],
            past_match_logs=[{"match_index": 1}],  # 使当前为第 2 局
        )

    def test_first_day_single_line(self):
        text = prompts.bid_request(self.make_obs(day=1))
        first_line = text.splitlines()[0]
        assert first_line.startswith("当前是第 2 局第 1 天。")
        assert "你的金币余额：7；" in first_line  # 首日不换行分句
        assert "昨日公布" not in text
        assert "已含昨日分红" not in text

    def test_later_day_three_lines_with_dividend_note(self):
        history = [{"day": 1, "winner": "乙", "voided_price_count": 0, "dividend": 4}]
        text = prompts.bid_request(self.make_obs(day=2, today_history=history))
        lines = text.splitlines()

        assert lines[0] == "当前是第 2 局第 2 天。"
        assert lines[1] == "昨日公布：乙 获胜，作废价位 0 个，分红 4 金币。"
        assert lines[2].startswith("你的金币余额：7（已含昨日分红）；")
        # 余额行必须在昨日公布之后
        assert text.index("昨日公布") < text.index("你的金币余额")

    def test_void_day_announcement_has_no_winner_wording(self):
        history = [{"day": 1, "winner": "流局", "voided_price_count": 2, "dividend": 0}]
        text = prompts.bid_request(self.make_obs(day=2, today_history=history))
        lines = text.splitlines()

        assert lines[1] == "昨日公布：流局（无分红），作废价位 2 个。"
        assert "获胜" not in lines[1]
        assert "已含昨日分红" in lines[2]

    def test_total_wins_in_first_day_line(self):
        text = prompts.bid_request(self.make_obs(day=1, total_wins=3))
        assert "你的累计总胜场：3（已含罚没）" in text.splitlines()[0]

    def test_total_wins_in_later_day_status_line(self):
        history = [{"day": 1, "winner": "乙", "voided_price_count": 0, "dividend": 4}]
        text = prompts.bid_request(self.make_obs(day=2, today_history=history, total_wins=5))
        assert "你的累计总胜场：5（已含罚没）" in text.splitlines()[2]


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


class TestPenaltyRendering:
    """胜场罚没：规则文本、局日志渲染、每日公布三处同步。"""

    def test_rules_text_mentions_penalty(self):
        assert "胜场罚没" in prompts.RULES_TEXT

    def test_rules_text_anchors_penalty_to_total_wins(self):
        assert "累计总胜场" in prompts.RULES_TEXT
        assert "高于获胜价" in prompts.RULES_TEXT
        assert "永不看" in prompts.RULES_TEXT

    def test_rules_text_voided_price_only_above_winning_bid(self):
        assert "只统计高于最终获胜价" in prompts.RULES_TEXT
        assert "低于或等于获胜价的撞车一律不计入" in prompts.RULES_TEXT

    def test_match_log_renders_penalty_in_outcome_and_summary(self):
        day = make_day(1, [2, 2, 1, 0, 0, 0], "丙", 1, 1, 0)
        day.penalties = [("甲", 1)]
        log = make_log(days=[day], wins={"丙": 1})
        text = prompts.render_match_log(log)

        assert "丙 以 1 获胜；甲 被罚 1 胜场" in text
        assert "本局罚没胜场：甲 1" in text

    def test_match_log_without_penalty_has_no_penalty_wording(self):
        assert "罚没" not in prompts.render_match_log(make_log())

    def test_bid_request_announces_penalty(self):
        history = [{
            "day": 1, "winner": "丙", "voided_price_count": 1, "dividend": 0,
            "penalties": [{"player": "甲", "wins": 1}],
        }]
        text = prompts.bid_request(TestBidRequest.make_obs(day=2, today_history=history))
        assert "昨日公布：丙 获胜，作废价位 1 个，分红 0 金币，胜场罚没：甲 1 胜场。" in text

    def test_bid_request_without_penalty_omits_penalty_wording(self):
        history = [{"day": 1, "winner": "乙", "voided_price_count": 0, "dividend": 4,
                    "penalties": []}]
        text = prompts.bid_request(TestBidRequest.make_obs(day=2, today_history=history))
        announcement_line = text.splitlines()[1]
        assert "胜场罚没" not in announcement_line  # 昨日公布不提罚没（状态行的「已含罚没」不算）

    def test_bid_request_status_line_carries_constant_penalty_hint(self):
        # 恒定提示与具体数字无关：首日单行与后续日状态行都必须出现
        first_day = prompts.bid_request(TestBidRequest.make_obs(day=1))
        assert "罚没提示" in first_day.splitlines()[0]
        assert "作废价位同样只统计高于获胜价的撞车" in first_day.splitlines()[0]

        history = [{"day": 1, "winner": "乙", "voided_price_count": 0, "dividend": 4,
                    "penalties": []}]
        later_day = prompts.bid_request(TestBidRequest.make_obs(day=2, today_history=history))
        status_line = later_day.splitlines()[2]
        assert "罚没提示" in status_line
        assert "作废价位同样只统计高于获胜价的撞车" in status_line
