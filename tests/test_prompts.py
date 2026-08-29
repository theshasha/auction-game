"""提示词文案与表格渲染的测试：字数软要求、加赛预告、发言拼接空行、表格格式。"""

import prompts
from models import BidRecord, DayResult, MatchLog, Observation, StandingsEntry

NAMES = ["甲", "乙", "丙", "丁", "戊", "己"]


def make_day(day, bids, pool, winner, winning_bid, collision_count, wins_taken=0,
             is_void=False, transfer_recipient=None, transfer_wins=0):
    return DayResult(
        day=day,
        bids=[BidRecord(player=name, raw_bid=bid, bid=bid) for name, bid in zip(NAMES, bids)],
        pool=pool,
        winner=winner,
        winning_bid=winning_bid,
        wins_taken=wins_taken,
        transfer_recipient=transfer_recipient,
        transfer_wins=transfer_wins,
        collision_price_count=collision_count,
        is_void=is_void,
    )


def make_log(match_index=1, is_playoff=False, days=None, wins=None):
    return MatchLog(
        match_index=match_index,
        is_playoff=is_playoff,
        days=days if days is not None else [make_day(1, [5, 4, 3, 2, 1, 0], 1, "甲", 5, 0, 1)],
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


class TestSystemPrompt:
    """system 提示词：规则全文 + 身份 + 人设 + 全员公开的赛前预测 + 人设作用范围。"""

    def test_includes_persona_and_scope(self):
        system = prompts.build_system_prompt(
            "甲", "傲娇大小姐", "自称本小姐。", [(name, 10) for name in NAMES], NAMES
        )
        assert "你叫 甲" in system
        assert "傲娇大小姐：自称本小姐。" in system
        # 人设只约束公开发言，出价推理明确要求保持冷静认真
        assert "不需要维持角色口吻" in system

    def test_predictions_public_to_everyone(self):
        predictions = [("甲", 50), ("乙", 20), ("丙", 10), ("丁", 10), ("戊", 10), ("己", 0)]
        for name in NAMES:
            system = prompts.build_system_prompt(name, "人设", "正文", predictions, NAMES)
            assert "甲 50%" in system and "己 0%" in system  # 全员可见，含他人与自己的
        system = prompts.build_system_prompt("甲", "人设", "正文", predictions, NAMES)
        assert "乙、丙、丁、戊、己" in system  # 其他五位玩家名单


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


class TestSpeechBlockFormat:
    """公开发言：先规划策略、末尾定界符块【公开发言】…【发言结束】，附发言免责句；观众总结不动。

    定界符取代裸 JSON：发言是自由文本，JSON 字符串要求转义双引号，
    实证模型在发言内嵌未转义的 " 导致解析失败（旧 JSON 格式解析仍兼容，见 ai_player）。
    """

    def test_public_speech_requests_ask_for_speech_block(self):
        standings = make_standings({n: [0] for n in NAMES})
        for text in (
            prompts.opening_speech_request(),
            prompts.match_end_speech_request(make_log(), standings, True),
            prompts.final_speech_request(standings, make_log(match_index=5)),
        ):
            assert "【公开发言】" in text
            assert "【发言结束】" in text
            assert "不构成对你后续出价的任何约束" in text

    def test_final_summary_stays_plain_text(self):
        text = prompts.final_summary_request({"乙": "感言"})
        assert "【公开发言】" not in text
        assert "纯文本" in text

    def test_retry_speech_request(self):
        text = prompts.retry_speech_request()
        assert "缺少合法的发言块" in text
        assert "【公开发言】" in text and "【发言结束】" in text


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
        assert "你的金币 6，本局已获胜场 0，今日奖池 1 个胜场，出价上限 6" in text
        assert '{"bid":' in text
        assert "```json" not in text  # 示例无围栏，避免模型误以为必须包代码块

    def test_next_match_plan_asks_for_day1_bid(self):
        text = prompts.next_match_plan_request({"乙": "发言"})
        assert "下一局第 1 天的出价" in text
        assert "你的金币 6，本局已获胜场 0，今日奖池 1 个胜场，出价上限 6" in text
        assert '{"bid":' in text
        assert "```json" not in text


class TestBidRequest:
    """每日出价消息：首句「当前是」、昨日公布带获胜价、状态行带奖池与工资标注。"""

    @staticmethod
    def make_obs(day=1, today_history=None, total_wins=0, pool=1, coins=7,
                 my_last_bid=None, my_last_deduction=None,
                 my_last_collided=False, my_last_bid_illegal=False):
        return Observation(
            my_name="甲",
            my_coins=coins,
            my_match_wins=0,
            my_total_wins=total_wins,
            day=day,
            today_pool=pool,
            today_history=today_history or [],
            standings=[],
            past_match_logs=[{"match_index": 1}],  # 使当前为第 2 局
            my_last_bid=my_last_bid,
            my_last_deduction=my_last_deduction,
            my_last_collided=my_last_collided,
            my_last_bid_illegal=my_last_bid_illegal,
        )

    def test_first_day_single_line(self):
        text = prompts.bid_request(self.make_obs(day=1))
        first_line = text.splitlines()[0]
        assert first_line.startswith("当前是第 2 局第 1 天。")
        # 首日不换行分句；状态行附「今日可出最高价」（= min(上限, 余额)），防止模型自己算错
        assert "今日奖池：1 个胜场；你的金币余额：7（今日可出最高价：6）；" in first_line
        assert "昨日公布" not in text
        assert "已含今日工资" not in text  # 第 1 天没有工资

    def test_max_affordable_bid_capped_by_balance(self):
        # 余额 4 低于上限 6 时，可出最高价明示为 4
        text = prompts.bid_request(self.make_obs(day=1, coins=4))
        assert "你的金币余额：4（今日可出最高价：4）；" in text.splitlines()[0]

    def test_later_day_three_lines_with_salary_note(self):
        history = [{"day": 1, "winner": "乙", "winning_bid": 5, "pool": 1, "collision_price_count": 0}]
        text = prompts.bid_request(self.make_obs(day=2, today_history=history))
        lines = text.splitlines()

        assert lines[0] == "当前是第 2 局第 2 天。"
        assert lines[1] == "昨日公布：乙 以 5 获胜，赢得 1 个胜场，撞车价位 0 个。"
        assert lines[2].startswith("今日奖池：1 个胜场；你的金币余额：7（已含今日工资，今日可出最高价：6）；")
        # 余额行必须在昨日公布之后
        assert text.index("昨日公布") < text.index("你的金币余额")

    def test_void_day_announcement_has_no_winner_wording(self):
        history = [{"day": 1, "winner": "流局", "winning_bid": None, "pool": 1, "collision_price_count": 2}]
        text = prompts.bid_request(self.make_obs(day=2, today_history=history, pool=2))
        lines = text.splitlines()

        assert lines[1] == "昨日公布：流局，奖池 1 个胜场并入今日，撞车价位 2 个。"
        assert "获胜" not in lines[1]
        assert "已含今日工资" in lines[2]
        assert "今日奖池：2 个胜场" in lines[2]  # 并入后的奖池

    def test_cap_tax_announcement_shows_post_tax_wins(self):
        # 获胜价 6 触发冲顶税、无转判对象：昨日公布显式展示奖池与税后实得
        history = [{
            "day": 1, "winner": "乙", "winning_bid": 6, "pool": 3,
            "wins_taken": 2, "transfer_recipient": None, "transfer_wins": 0,
            "collision_price_count": 0,
        }]
        text = prompts.bid_request(self.make_obs(day=2, today_history=history))
        lines = text.splitlines()

        assert lines[1] == "昨日公布：乙 以 6 获胜，奖池 3 个胜场，缴冲顶税后实得 2 个，撞车价位 0 个。"

    def test_cap_tax_announcement_shows_transfer_recipient(self):
        # 获胜价 6 且有次高唯一出价者：昨日公布带转判获得者与转判胜场
        history = [{
            "day": 1, "winner": "乙", "winning_bid": 6, "pool": 2,
            "wins_taken": 1, "transfer_recipient": "丁", "transfer_wins": 1,
            "collision_price_count": 1,
        }]
        text = prompts.bid_request(self.make_obs(day=2, today_history=history))
        lines = text.splitlines()

        assert lines[1] == ("昨日公布：乙 以 6 获胜，奖池 2 个胜场，缴冲顶税后实得 1 个，"
                            "转判 丁 获得 1 个（转判获得者照常扣除自己的出价），撞车价位 1 个。")

    def test_total_wins_in_first_day_line(self):
        text = prompts.bid_request(self.make_obs(day=1, total_wins=3))
        assert "你的累计总胜场：3。" in text.splitlines()[0]

    def test_total_wins_in_later_day_status_line(self):
        history = [{"day": 1, "winner": "乙", "winning_bid": 5, "pool": 1, "collision_price_count": 0}]
        text = prompts.bid_request(self.make_obs(day=2, today_history=history, total_wins=5))
        assert "你的累计总胜场：5。" in text.splitlines()[2]


class TestMyLastDayLine:
    """「昨日你的结果」行：直接告知昨日自己的出价、结局与扣款（零信息泄露，
    自己的出价与余额变化本来就可推出），位于昨日公布之后、今日状态行之前。"""

    make_obs = staticmethod(TestBidRequest.make_obs)

    @staticmethod
    def history(**overrides):
        base = {"day": 1, "winner": "乙", "winning_bid": 5, "pool": 1, "wins_taken": 1,
                "transfer_recipient": None, "transfer_wins": 0, "collision_price_count": 0}
        base.update(overrides)
        return [base]

    def test_no_line_when_last_bid_unknown(self):
        # my_last_bid 为 None（如旧调用方未填）时不渲染该行
        text = prompts.bid_request(self.make_obs(day=2, today_history=self.history()))
        assert "昨日你的结果" not in text

    def test_winner_without_tax(self):
        text = prompts.bid_request(self.make_obs(
            day=2, today_history=self.history(winner="甲", winning_bid=5, wins_taken=1),
            my_last_bid=5, my_last_deduction=5,
        ))
        assert "昨日你的结果：出价 5，唯一最高出价获胜，赢得 1 个胜场，扣除 5。" in text

    def test_winner_with_cap_tax(self):
        text = prompts.bid_request(self.make_obs(
            day=2, today_history=self.history(winner="甲", winning_bid=6, wins_taken=1),
            my_last_bid=6, my_last_deduction=6,
        ))
        assert "昨日你的结果：出价 6，唯一最高出价获胜，缴冲顶税后实得 1 个胜场，扣除 6。" in text

    def test_transfer_recipient_pays(self):
        # 转判获得者照常扣款——实证有模型把转判当成「免费获赠」
        text = prompts.bid_request(self.make_obs(
            day=2, today_history=self.history(winner="乙", winning_bid=6, wins_taken=1,
                                              transfer_recipient="甲", transfer_wins=1),
            my_last_bid=3, my_last_deduction=3,
        ))
        assert "昨日你的结果：出价 3，获转判 1 个胜场，照常扣除 3。" in text

    def test_collided(self):
        text = prompts.bid_request(self.make_obs(
            day=2, today_history=self.history(),
            my_last_bid=4, my_last_deduction=4, my_last_collided=True,
        ))
        assert "昨日你的结果：出价 4，与他人撞车，扣除 4。" in text

    def test_unique_not_won_refunded(self):
        text = prompts.bid_request(self.make_obs(
            day=2, today_history=self.history(),
            my_last_bid=2, my_last_deduction=0,
        ))
        assert "昨日你的结果：出价 2，是唯一出价但未获胜，全额退还。" in text

    def test_void_day_all_collided(self):
        text = prompts.bid_request(self.make_obs(
            day=2, today_history=self.history(winner="流局", winning_bid=None, wins_taken=0),
            my_last_bid=1, my_last_deduction=1, my_last_collided=True,
        ))
        assert "昨日你的结果：出价 1，当天流局全员撞车，扣除 1。" in text

    def test_illegal_bid_recorded_as_zero(self):
        text = prompts.bid_request(self.make_obs(
            day=2, today_history=self.history(),
            my_last_bid=0, my_last_deduction=0, my_last_bid_illegal=True,
        ))
        assert "昨日你的结果：出价非法按规则记 0，是唯一出价但未获胜，全额退还。" in text

    def test_line_position_between_announcement_and_status(self):
        text = prompts.bid_request(self.make_obs(
            day=2, today_history=self.history(),
            my_last_bid=2, my_last_deduction=0,
        ))
        assert text.index("昨日公布") < text.index("昨日你的结果") < text.index("今日奖池")


class TestRulesReminder:
    """局末发言请求在展示局日志前重申结算四步（精简版），其余请求不带。"""

    def test_match_end_speech_contains_reminder_before_log(self):
        text = prompts.match_end_speech_request(
            make_log(), make_standings({n: [0] for n in NAMES}), True
        )
        assert "【规则要点重申】" in text
        assert text.index("【规则要点重申】") < text.index("本局完整出价日志")

    def test_reminder_covers_four_steps_and_transfer_payment(self):
        reminder = prompts.RULES_REMINDER
        assert "撞车者全部扣除出价" in reminder
        assert "唯一出价中最高者获胜" in reminder
        assert "转判获得者照常扣除自己的出价，并非免费获赠" in reminder
        assert "流局日撞车照扣" in reminder
        assert "全额退还" in reminder

    def test_other_requests_have_no_reminder(self):
        standings = make_standings({n: [0] for n in NAMES})
        assert "【规则要点重申】" not in prompts.next_match_plan_request({"乙": "发言"})
        assert "【规则要点重申】" not in prompts.final_speech_request(standings, make_log(match_index=5))


class TestRenderMatchLog:
    def test_renders_markdown_table_with_player_columns(self):
        log = make_log(days=[
            make_day(1, [5, 4, 3, 2, 1, 0], 1, "甲", 5, 0, 1),
            make_day(2, [3, 3, 2, 2, 2, 0], 1, "己", 0, 2, 1),
        ])
        text = prompts.render_match_log(log)
        lines = text.splitlines()

        assert lines[0] == "| 天 | 奖池 | 甲 | 乙 | 丙 | 丁 | 戊 | 己 | 结果 | 撞车价位 |"
        assert lines[2] == "| 1 | 1 | 5 | 4 | 3 | 2 | 1 | 0 | 甲 以 5 获胜 | 0 |"
        assert lines[3] == "| 2 | 1 | 3 | 3 | 2 | 2 | 2 | 0 | 己 以 0 获胜 | 2 |"
        assert "本局胜场：" in text

    def test_cap_tax_shown_in_outcome(self):
        # 获胜价 6 触发冲顶税：结果列显式展示 奖池→税后；无转判对象时不提转判
        log = make_log(days=[make_day(3, [6, 5, 5, 3, 3, 3], 2, "甲", 6, 2, 1)])
        text = prompts.render_match_log(log)
        assert "| 3 | 2 | 6 | 5 | 5 | 3 | 3 | 3 | 甲 以 6 获胜（冲顶税：2→1） | 2 |" in text

    def test_transfer_shown_in_outcome(self):
        # 获胜价 6 且有次高唯一出价者：结果列带「转判 X 得 N，照扣其出价」
        log = make_log(days=[make_day(3, [6, 5, 5, 3, 2, 0], 2, "甲", 6, 1, 1,
                                      transfer_recipient="丁", transfer_wins=1)])
        text = prompts.render_match_log(log)
        assert "| 3 | 2 | 6 | 5 | 5 | 3 | 2 | 0 | 甲 以 6 获胜（冲顶税：2→1，转判 丁 得 1，照扣其出价） | 1 |" in text

    def test_void_day_row(self):
        log = make_log(days=[make_day(1, [2, 2, 2, 1, 1, 1], 1, None, None, 2, is_void=True)])
        text = prompts.render_match_log(log)
        assert "| 1 | 1 | 2 | 2 | 2 | 1 | 1 | 1 | 流局（奖池并入次日） | 2 |" in text

    def test_day5_void_row_marks_pool_forfeited(self):
        log = make_log(days=[make_day(5, [2, 2, 2, 1, 1, 1], 8, None, None, 2, is_void=True)])
        text = prompts.render_match_log(log)
        assert "| 5 | 8 | 2 | 2 | 2 | 1 | 1 | 1 | 流局（奖池作废） | 2 |" in text

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


class TestRulesText:
    """规则全文单一来源是 RULES.md：RULES_TEXT 即文件内容，要点与文件逐条对齐。"""

    def test_rules_text_loaded_from_rules_md(self):
        from pathlib import Path
        rules_md = (Path(prompts.__file__).parent / "RULES.md").read_text(encoding="utf-8").strip()
        assert prompts.RULES_TEXT == rules_md

    def test_core_mechanics_present(self):
        text = prompts.RULES_TEXT
        assert "每局固定 5 天" in text
        assert "第 1 天 1 个，第 2~5 天各 2 个" in text
        assert "奖池并入次日" in text
        assert "每天开始时每人发放 3 枚金币（工资）" in text
        assert "出价为 0 到 6 的整数" in text

    def test_settlement_four_steps_present(self):
        text = prompts.RULES_TEXT
        assert "判定撞车" in text
        assert "判定胜负" in text
        assert "金币结算" in text
        assert "其余玩家全额退还出价" in text
        assert "流局日同样扣除" in text
        assert "系统没收" in text

    def test_cap_tax_transfer_present(self):
        text = prompts.RULES_TEXT
        assert "冲顶税与转判" in text
        assert "获胜价为 6" in text
        assert "向上取整" in text and "向下取整" in text
        assert "次高唯一出价者" in text
        assert "以 0~5 获胜则获得奖池全部胜场" in text
        # 强调句：实证有模型把转判当成「免费获赠」
        assert "转判并非免费获赠" in text

    def test_settlement_examples_present(self):
        text = prompts.RULES_TEXT
        # 示例一：6 唯一获胜触发转判；示例二：最高出价撞车、次高唯一价获胜
        assert "结算示例一" in text and "结算示例二" in text
        assert "次高唯一出价是 3，出 3 者获得 1 个胜场" in text
        assert "唯一出价中最高的是 2，出 2 者获胜" in text

    def test_publication_includes_winning_bid_and_transfer(self):
        text = prompts.RULES_TEXT
        assert "获胜者身份与获胜价" in text
        assert "撞车价位的个数" in text
        assert "转判获得者身份（若有）" in text
