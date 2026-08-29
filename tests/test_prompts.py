"""提示词文案与表格渲染的测试：字数软要求、加赛预告、发言拼接空行、表格格式、
超级出价在状态行/昨日公布/昨日结果/局日志中的呈现。"""

import prompts
from models import BidRecord, DayResult, MatchLog, Observation, StandingsEntry

NAMES = ["甲", "乙", "丙", "丁", "戊", "己"]


def make_day(day, bids, pool, winner, winning_bid, collision_count, wins_taken=0,
             is_void=False, super_bids=None, winning_is_super=False):
    """构造一天结果。bids 为六人正常出价；super_bids 为 {玩家: 超级出价}，
    每个使用者在出价表里追加一笔 is_super=True 的记录。"""
    records = [BidRecord(player=name, raw_bid=bid, bid=bid) for name, bid in zip(NAMES, bids)]
    for name, super_bid in (super_bids or {}).items():
        records.append(
            BidRecord(player=name, raw_bid=super_bid, bid=super_bid, is_super=True)
        )
    return DayResult(
        day=day,
        bids=records,
        pool=pool,
        winner=winner,
        winning_bid=winning_bid,
        wins_taken=wins_taken,
        winning_is_super=winning_is_super,
        super_users=list((super_bids or {}).keys()),
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
    """规划请求与下一局第 1 天出价合并：附恒定首日状态与 JSON 出价格式（含超级出价）。"""

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

    def test_bid_format_hint_covers_super_bid(self):
        text = prompts.bid_format_hint()
        assert '"super_bid"' in text
        assert "不受余额限制" in text
        assert "不使用超级出价就不要包含 super_bid 字段" in text
        assert "```json" not in text


class TestBidRequest:
    """每日出价消息：首句「当前是」、昨日公布带获胜价与超级出价使用情况、
    状态行带奖池/工资标注/超级出价状态。"""

    @staticmethod
    def make_obs(day=1, today_history=None, total_wins=0, pool=1, coins=7,
                 my_last_bid=None, my_last_deduction=None,
                 my_last_collided=False, my_last_bid_illegal=False,
                 my_super_available=True, my_super_used_day=None,
                 my_last_super_bid=None, my_last_super_collided=False):
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
            my_super_available=my_super_available,
            my_super_used_day=my_super_used_day,
            my_last_bid=my_last_bid,
            my_last_deduction=my_last_deduction,
            my_last_collided=my_last_collided,
            my_last_bid_illegal=my_last_bid_illegal,
            my_last_super_bid=my_last_super_bid,
            my_last_super_collided=my_last_super_collided,
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

    def test_super_status_available_in_first_day_line(self):
        text = prompts.bid_request(self.make_obs(day=1))
        assert "超级出价：可用（本局仅 1 次" in text.splitlines()[0]
        assert "不受余额限制" in text.splitlines()[0]

    def test_super_status_used_shows_day(self):
        history = [{"day": 1, "winner": "乙", "winning_bid": 5, "pool": 1, "wins_taken": 1,
                    "super_users": [], "collision_price_count": 0}]
        text = prompts.bid_request(self.make_obs(
            day=2, today_history=history, my_super_available=False, my_super_used_day=1,
        ))
        assert "超级出价：已用（本局第 1 天）" in text

    def test_later_day_three_lines_with_salary_note(self):
        history = [{"day": 1, "winner": "乙", "winning_bid": 5, "pool": 1, "wins_taken": 1,
                    "super_users": [], "collision_price_count": 0}]
        text = prompts.bid_request(self.make_obs(day=2, today_history=history))
        lines = text.splitlines()

        assert lines[0] == "当前是第 2 局第 2 天。"
        assert lines[1] == "昨日公布：乙 以 5 获胜，赢得 1 个胜场，撞车价位 0 个。"
        assert lines[2].startswith("今日奖池：1 个胜场；你的金币余额：7（已含今日工资，今日可出最高价：6）；")
        # 余额行必须在昨日公布之后
        assert text.index("昨日公布") < text.index("你的金币余额")

    def test_super_users_announcement_names_only(self):
        # 超级出价使用情况：只公布谁使用了，不公布数值
        history = [{"day": 1, "winner": "乙", "winning_bid": 5, "pool": 1, "wins_taken": 1,
                    "super_users": ["丙", "丁"], "collision_price_count": 0}]
        text = prompts.bid_request(self.make_obs(day=2, today_history=history))
        lines = text.splitlines()

        assert lines[1].endswith("昨日使用超级出价：丙、丁（数值不公布）。")

    def test_no_super_users_no_announcement(self):
        history = [{"day": 1, "winner": "乙", "winning_bid": 5, "pool": 1, "wins_taken": 1,
                    "super_users": [], "collision_price_count": 0}]
        text = prompts.bid_request(self.make_obs(day=2, today_history=history))
        assert "超级出价：" not in text.splitlines()[1]  # 昨日公布行不含使用情况

    def test_void_day_announcement_has_no_winner_wording(self):
        history = [{"day": 1, "winner": "流局", "winning_bid": None, "pool": 1, "wins_taken": 0,
                    "super_users": [], "collision_price_count": 2}]
        text = prompts.bid_request(self.make_obs(day=2, today_history=history, pool=2))
        lines = text.splitlines()

        assert lines[1] == "昨日公布：流局，奖池 1 个胜场并入今日，撞车价位 2 个。"
        assert "获胜" not in lines[1]
        assert "已含今日工资" in lines[2]
        assert "今日奖池：2 个胜场" in lines[2]  # 并入后的奖池

    def test_total_wins_in_first_day_line(self):
        text = prompts.bid_request(self.make_obs(day=1, total_wins=3))
        assert "你的累计总胜场：3；" in text.splitlines()[0]

    def test_total_wins_in_later_day_status_line(self):
        history = [{"day": 1, "winner": "乙", "winning_bid": 5, "pool": 1, "wins_taken": 1,
                    "super_users": [], "collision_price_count": 0}]
        text = prompts.bid_request(self.make_obs(day=2, today_history=history, total_wins=5))
        assert "你的累计总胜场：5；" in text.splitlines()[2]


class TestMyLastDayLine:
    """「昨日你的结果」行：直接告知昨日自己的出价、结局与扣款（零信息泄露，
    自己的出价与余额变化本来就可推出），位于昨日公布之后、今日状态行之前。
    正常出价与超级出价的结局分段给出（超级出价任何结局都不扣款）。"""

    make_obs = staticmethod(TestBidRequest.make_obs)

    @staticmethod
    def history(**overrides):
        base = {"day": 1, "winner": "乙", "winning_bid": 5, "pool": 1, "wins_taken": 1,
                "super_users": [], "collision_price_count": 0}
        base.update(overrides)
        return [base]

    def test_no_line_when_last_bid_unknown(self):
        # my_last_bid 为 None（如旧调用方未填）时不渲染该行
        text = prompts.bid_request(self.make_obs(day=2, today_history=self.history()))
        assert "昨日你的结果" not in text

    def test_winner_with_normal_bid(self):
        text = prompts.bid_request(self.make_obs(
            day=2, today_history=self.history(winner="甲", winning_bid=5, wins_taken=1),
            my_last_bid=5, my_last_deduction=5,
        ))
        assert "昨日你的结果：正常出价 5，唯一最高出价获胜，赢得 1 个胜场，扣除 5。" in text

    def test_collided(self):
        text = prompts.bid_request(self.make_obs(
            day=2, today_history=self.history(),
            my_last_bid=4, my_last_deduction=4, my_last_collided=True,
        ))
        assert "昨日你的结果：正常出价 4，与他人撞车，扣除 4。" in text

    def test_unique_not_won_refunded(self):
        text = prompts.bid_request(self.make_obs(
            day=2, today_history=self.history(),
            my_last_bid=2, my_last_deduction=0,
        ))
        assert "昨日你的结果：正常出价 2，是唯一出价但未获胜，全额退还。" in text

    def test_void_day_all_collided(self):
        text = prompts.bid_request(self.make_obs(
            day=2, today_history=self.history(winner="流局", winning_bid=None, wins_taken=0),
            my_last_bid=1, my_last_deduction=1, my_last_collided=True,
        ))
        assert "昨日你的结果：正常出价 1，当天流局全员撞车，扣除 1。" in text

    def test_illegal_bid_recorded_as_zero(self):
        text = prompts.bid_request(self.make_obs(
            day=2, today_history=self.history(),
            my_last_bid=0, my_last_deduction=0, my_last_bid_illegal=True,
        ))
        assert "昨日你的结果：正常出价非法按规则记 0，是唯一出价但未获胜，全额退还。" in text

    def test_super_bid_won_free_of_charge(self):
        # 凭超级出价获胜：赢得胜场但不扣金币；正常出价分段照常（唯一未中退还）
        text = prompts.bid_request(self.make_obs(
            day=2,
            today_history=self.history(winner="甲", winning_bid=6, wins_taken=1,
                                       super_users=["甲"]),
            my_last_bid=4, my_last_deduction=0,
            my_last_super_bid=6, my_super_available=False, my_super_used_day=1,
        ))
        assert ("昨日你的结果：正常出价 4，是唯一出价但未获胜，全额退还，"
                "超级出价 6 唯一最高获胜，赢得 1 个胜场，不扣金币。") in text

    def test_super_bid_collided_no_charge(self):
        # 超级出价撞车（狙杀场景）：不扣款；正常出价分段照常
        text = prompts.bid_request(self.make_obs(
            day=2,
            today_history=self.history(super_users=["甲"]),
            my_last_bid=4, my_last_deduction=0,
            my_last_super_bid=6, my_last_super_collided=True,
            my_super_available=False, my_super_used_day=1,
        ))
        assert ("昨日你的结果：正常出价 4，是唯一出价但未获胜，全额退还，"
                "超级出价 6 撞车，不扣款。") in text

    def test_super_bid_unique_not_won(self):
        text = prompts.bid_request(self.make_obs(
            day=2,
            today_history=self.history(super_users=["甲"]),
            my_last_bid=4, my_last_deduction=4, my_last_collided=True,
            my_last_super_bid=3, my_super_available=False, my_super_used_day=1,
        ))
        assert ("昨日你的结果：正常出价 4，与他人撞车，扣除 4，"
                "超级出价 3 是唯一出价但未获胜，不扣款。") in text

    def test_line_position_between_announcement_and_status(self):
        text = prompts.bid_request(self.make_obs(
            day=2, today_history=self.history(),
            my_last_bid=2, my_last_deduction=0,
        ))
        assert text.index("昨日公布") < text.index("昨日你的结果") < text.index("今日奖池")


class TestRulesReminder:
    """局末发言请求在展示局日志前重申结算三步（精简版），其余请求不带。"""

    def test_match_end_speech_contains_reminder_before_log(self):
        text = prompts.match_end_speech_request(
            make_log(), make_standings({n: [0] for n in NAMES}), True
        )
        assert "【规则要点重申】" in text
        assert text.index("【规则要点重申】") < text.index("本局完整出价日志")

    def test_reminder_covers_three_steps_and_super_bid(self):
        reminder = prompts.RULES_REMINDER
        assert "两笔及以上即撞车" in reminder
        assert "唯一出价中最高的一笔获胜" in reminder
        assert "全额退还" in reminder
        # 超级出价要点：每局一次、不扣金币、使用情况只公布谁用了
        assert "超级出价每人每局一次" in reminder
        assert "不公布其数值" in reminder

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

    def test_super_bid_shown_in_cell(self):
        # 使用超级出价的玩家在出价列写作「正常价+超N」
        log = make_log(days=[
            make_day(1, [6, 4, 5, 3, 3, 2], 1, "丙", 5, 2, 1, super_bids={"乙": 6}),
        ])
        text = prompts.render_match_log(log)
        assert "| 1 | 1 | 6 | 4+超6 | 5 | 3 | 3 | 2 | 丙 以 5 获胜 | 2 |" in text

    def test_super_bid_win_marked_free_of_charge(self):
        # 凭超级出价获胜：结果列标注不扣金币
        log = make_log(days=[
            make_day(1, [3, 4, 5, 3, 2, 1], 1, "甲", 6, 1, 1,
                     super_bids={"甲": 6}, winning_is_super=True),
        ])
        text = prompts.render_match_log(log)
        assert "甲 以 6 获胜（凭超级出价，不扣金币）" in text

    def test_void_day_row(self):
        log = make_log(days=[make_day(1, [2, 2, 2, 1, 1, 1], 1, None, None, 2, is_void=True)])
        text = prompts.render_match_log(log)
        assert "| 1 | 1 | 2 | 2 | 2 | 1 | 1 | 1 | 流局（奖池并入次日） | 2 |" in text

    def test_day5_void_row_marks_pool_forfeited(self):
        log = make_log(days=[make_day(5, [2, 2, 2, 1, 1, 1], 3, None, None, 2, is_void=True)])
        text = prompts.render_match_log(log)
        assert "| 5 | 3 | 2 | 2 | 2 | 1 | 1 | 1 | 流局（奖池作废） | 2 |" in text

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


class TestInvalidBidRetry:
    """数值非法（解析成功但违反出价规则）的提醒重试：写清具体违规原因。"""

    def test_includes_reason_and_format_hint(self):
        text = prompts.invalid_bid_retry_request("你的出价 6 超过当前余额 4")
        assert "你的出价 6 超过当前余额 4" in text
        assert "请重新出价" in text
        assert '{"bid":' in text  # 附出价格式提示


class TestRulesText:
    """规则全文单一来源是 RULES.md：RULES_TEXT 即文件内容，要点与文件逐条对齐。"""

    def test_rules_text_loaded_from_rules_md(self):
        from pathlib import Path
        rules_md = (Path(prompts.__file__).parent / "RULES.md").read_text(encoding="utf-8").strip()
        assert prompts.RULES_TEXT == rules_md

    def test_core_mechanics_present(self):
        text = prompts.RULES_TEXT
        assert "每局固定 5 天" in text
        assert "每天拍卖 1 个胜场，每局共 5 个胜场" in text
        assert "奖池并入次日" in text
        assert "每天开始时每人发放 3 枚金币（工资）" in text
        assert "出价为 0 到 6 的整数" in text

    def test_super_bid_rules_present(self):
        text = prompts.RULES_TEXT
        assert "每名玩家每局拥有 1 次超级出价机会" in text
        assert "不受余额限制" in text
        assert "无论获胜、撞车还是未中，超级出价都不扣除金币" in text
        assert "每一笔单独计数" in text

    def test_settlement_three_steps_present(self):
        text = prompts.RULES_TEXT
        assert "判定撞车" in text
        assert "判定胜负" in text
        assert "金币结算" in text
        assert "其余正常出价全额退还" in text
        assert "流局日同样扣除" in text
        assert "系统没收" in text

    def test_no_cap_tax_anymore(self):
        text = prompts.RULES_TEXT
        assert "冲顶税" not in text
        assert "转判" not in text

    def test_settlement_examples_present(self):
        text = prompts.RULES_TEXT
        # 示例一：超级出价撞车、他人捡漏；示例二：最高出价撞车、次高唯一价获胜
        assert "结算示例一" in text and "结算示例二" in text
        assert "唯一出价中最高的是 5，丙获胜" in text
        assert "唯一出价中最高的是 2" in text

    def test_publication_includes_super_usage_without_value(self):
        text = prompts.RULES_TEXT
        assert "获胜者身份与获胜价" in text
        assert "撞车价位的个数" in text
        assert "只公布谁使用了，不公布其数值" in text
