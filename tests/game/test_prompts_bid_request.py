"""每日出价消息（bid_request）的测试：状态行、昨日公布、「昨日你的结果」行。
共享工厂（make_obs / history）见 conftest.py。"""

from dataclasses import replace

import pytest

from auction_game.ai import prompts
from auction_game.game.models import StandingsEntry
from tests.support.factories import TEST_GAME, history, make_obs


def make_prompt_obs(**kwargs):
    """bid_request 测试视角：past_match_logs 置一局，使消息显示「当前是第 2 局」。"""
    kwargs.setdefault("past_match_logs", [{"match_index": 1}])
    return make_obs(**kwargs)


class TestBidRequest:
    """每日出价消息：首句「当前是」、昨日公布带获胜价与昨日出价明细（全透明：
    全场正常出价数值 + 各人超级出价数值）、状态行带奖池/工资标注。"""

    def test_first_day_single_line(self):
        text = prompts.bid_request(make_prompt_obs(day=1, total_wins=3))
        first_line = text.splitlines()[0]
        assert first_line.startswith("当前是第 2 局第 1 天。")
        assert "今日奖池：1 个胜场；你的金币余额：7（正常出价上限：7）；" in first_line
        assert "昨日公布" not in text
        assert "已含今日工资" not in text  # 第 1 天没有工资
        assert "你的累计总胜场：3。" in first_line
        assert "当前全场金币" not in text  # 首日恒定余额不再单列公布

    @pytest.mark.parametrize("day", [1, 2])
    def test_live_totals_include_all_players(self, day):
        obs = make_prompt_obs(day=day, today_history=[history()] if day > 1 else [])
        obs.standings = [StandingsEntry("甲", 6, [3, 3]), StandingsEntry("乙", 4, [2, 2])]
        assert "全员实时总胜场：甲 6 胜、乙 4 胜。" in prompts.bid_request(obs)

    @pytest.mark.parametrize("match_index,visible", [(1, False), (2, True), (3, True), (4, True)])
    def test_remaining_wins_only_final_regular_and_playoffs(self, match_index, visible):
        obs = make_prompt_obs(day=2, pool=5, today_history=[history()])
        obs.game = replace(TEST_GAME, regular_matches=2, daily_pool=(2, 3, 4))
        obs.past_match_logs = [{}] * (match_index - 1)
        text = prompts.bid_request(obs)
        assert ("本局剩余可分配胜场：9" in text) == visible
        plan = prompts.next_match_plan_request([], obs.game, match_index)
        assert ("本局剩余可分配胜场：9" in plan) == visible
        assert "。。" not in plan
        assert plan.split("\n\n")[-2].endswith("。")

    def test_single_match_opening_and_final_day_remaining(self):
        game = replace(TEST_GAME, regular_matches=1, daily_pool=(2, 3))
        opening = prompts.opening_plan_request({}, game)
        assert "全员实时总胜场：全员 0 胜。" in opening
        assert "本局剩余可分配胜场：5" in opening
        obs = make_prompt_obs(day=2, pool=5, today_history=[history()])
        obs.game = game
        assert "本局剩余可分配胜场：5" in prompts.bid_request(obs)

    def test_max_affordable_bid_capped_by_balance(self):
        # 余额 4 低于上限时，可出最高价明示为 4
        text = prompts.bid_request(make_prompt_obs(day=1, coins=4))
        assert "你的金币余额：4（正常出价上限：4）；" in text.splitlines()[0]

    def test_later_day_three_lines_with_salary_note(self):
        text = prompts.bid_request(make_prompt_obs(
            day=2, today_history=[history()], total_wins=5, coins=10,
            all_coins={"甲": 10, "乙": 8, "丙": 9, "丁": 7, "戊": 6, "己": 5},
        ))
        lines = text.splitlines()

        assert lines[0] == "当前是第 2 局第 2 天。"
        assert lines[1] == (
            "昨日公布：乙 以 5 获胜，赢得 1 个胜场。"
            "昨日出价明细：甲 6、乙 5、丙 4、丁 3、戊 2、己 1。"
            "当前全场金币：甲 10、乙 8、丙 9、丁 7、戊 6、己 5。"
        )
        assert lines[2].startswith("今日奖池：1 个胜场；你的金币余额：10（已含今日工资，正常出价上限：7）；")
        assert "你的累计总胜场：5。" in lines[2]
        # 余额行必须在昨日公布之后
        assert text.index("昨日公布") < text.index("你的金币余额")

    def test_bids_detail_announcement_with_super_values_and_hits(self):
        # 昨日出价明细（全透明）：全场正常出价数值 + 各人超级出价数值与命中标注
        text = prompts.bid_request(make_prompt_obs(
            day=2,
            today_history=[history(
                bids_detail=[
                    {"player": "甲", "bid": 6, "super_bid": None},
                    {"player": "乙", "bid": 5, "super_bid": None},
                    {"player": "丙", "bid": 4, "super_bid": 6},
                    {"player": "丁", "bid": 3, "super_bid": 5},
                    {"player": "戊", "bid": 2, "super_bid": None},
                    {"player": "己", "bid": 1, "super_bid": None},
                ],
                super_uses=[{"player": "丙", "value": 6, "hit": True},
                            {"player": "丁", "value": 5, "hit": False}],
            )],
        ))
        lines = text.splitlines()

        assert "昨日出价明细：甲 6、乙 5、丙 4+超6（命中）、丁 3+超5（落空）、戊 2、己 1。" in lines[1]

    def test_void_day_announcement_has_no_winner_wording(self):
        text = prompts.bid_request(make_prompt_obs(
            day=2,
            today_history=[history(winner="流局", winning_bid=None, wins_taken=0)],
            pool=2,
        ))
        lines = text.splitlines()

        assert lines[1].startswith("昨日公布：流局，奖池 1 个胜场并入今日。")
        assert "以" not in lines[1].split("。")[0]
        assert "已含今日工资" in lines[2]
        assert "今日奖池：2 个胜场" in lines[2]  # 并入后的奖池


class TestMyLastDayLine:
    """「昨日你的结果」行：直接告知昨日自己的出价、结局与扣款（零信息泄露，
    自己的出价与余额变化本来就可推出），位于昨日公布之后、今日状态行之前。
    正常出价与超级出价的结局分段给出：正常出价有获胜/被狙/撞车/流局/未中退款
    五种结局；超级出价固定扣 2 金币成本，命中再 +3 悬赏。"""

    @pytest.mark.parametrize(
        "obs_kwargs, history_overrides, expected",
        [
            (dict(my_last_bid=5, my_last_deduction=5),
             dict(winner="甲", winning_bid=5, wins_taken=1),
             "昨日你的结果：正常出价 5，唯一最高出价获胜，赢得 1 个胜场，扣除 5。"),
            (dict(my_last_bid=4, my_last_deduction=4, my_last_collided=True),
             {},
             "昨日你的结果：正常出价 4，与他人撞车，扣除 4。"),
            (dict(my_last_bid=2, my_last_deduction=0),
             {},
             "昨日你的结果：正常出价 2，未撞车但也不是最高，全额退还。"),
            # 被超级出价狙中：按撞车扣款，与互撞分开表述
            (dict(my_last_bid=6, my_last_deduction=6,
                  my_last_collided=True, my_last_sniped=True),
             {},
             "昨日你的结果：正常出价 6，被超级出价狙中，扣除 6。"),
            (dict(my_last_bid=1, my_last_deduction=1, my_last_collided=True),
             dict(winner="流局", winning_bid=None, wins_taken=0),
             "昨日你的结果：正常出价 1，当天流局（正常出价全部撞车），扣除 1。"),
            # 整单口径：非法提交整单记为正常出价 0
            (dict(my_last_bid=0, my_last_deduction=0, my_last_bid_illegal=True),
             {},
             "昨日你的结果：提交非法，按规则整单记为正常出价 0，未撞车但也不是最高，全额退还。"),
            # 超级出价落空：无效果，但照扣 2 金币成本；正常出价分段照常
            (dict(my_last_bid=4, my_last_deduction=0, my_last_super_bid=6),
             dict(super_uses=[{"player": "甲", "value": 6, "hit": False}]),
             "昨日你的结果：正常出价 4，未撞车但也不是最高，全额退还，"
             "超级出价 6 落空，扣除 2 金币成本。"),
            # 超级出价命中：扣 2 成本，获 3 金币悬赏
            (dict(my_last_bid=4, my_last_deduction=0, my_last_super_bid=6,
                  my_last_super_hit=True),
             dict(super_uses=[{"player": "甲", "value": 6, "hit": True}]),
             "昨日你的结果：正常出价 4，未撞车但也不是最高，全额退还，"
             "超级出价 6 命中，扣除 2 金币成本，获得 3 金币悬赏。"),
            (dict(my_last_bid=4, my_last_deduction=4, my_last_collided=True,
                  my_last_super_bid=3),
             dict(super_uses=[{"player": "甲", "value": 3, "hit": False}]),
             "昨日你的结果：正常出价 4，与他人撞车，扣除 4，"
             "超级出价 3 落空，扣除 2 金币成本。"),
        ],
        ids=["won", "collided", "refunded", "sniped", "void", "illegal-zero",
             "super-miss", "super-hit-bounty", "super-miss-collided"],
    )
    def test_last_day_line_segments(self, obs_kwargs, history_overrides, expected):
        text = prompts.bid_request(make_prompt_obs(
            day=2, today_history=[history(**history_overrides)], **obs_kwargs,
        ))
        assert expected in text

    def test_no_line_when_last_bid_unknown(self):
        # my_last_bid 为 None（如旧调用方未填）时不渲染该行
        text = prompts.bid_request(make_prompt_obs(day=2, today_history=[history()]))
        assert "昨日你的结果" not in text

    def test_line_position_between_announcement_and_status(self):
        text = prompts.bid_request(make_prompt_obs(
            day=2, today_history=[history()],
            my_last_bid=2, my_last_deduction=0,
        ))
        assert text.index("昨日公布") < text.index("昨日你的结果") < text.index("今日奖池")

    def test_custom_game_sets_daily_limit_cost_and_bounty(self):
        obs = make_prompt_obs(
            day=2, coins=12, today_history=[history()],
            my_last_bid=4, my_last_deduction=0, my_last_super_bid=9,
            my_last_super_hit=True,
        )
        obs.game = replace(TEST_GAME, max_bid=9, super_cost=3, super_bounty=5)
        text = prompts.bid_request(obs)
        assert "正常出价上限：9" in text
        assert "超级出价 9 命中，扣除 3 金币成本，获得 5 金币悬赏" in text
        assert "正常出价加 3 金币成本" in text

    @pytest.mark.parametrize("days", [1, 3])
    def test_final_day_reminds_coins_reset(self, days):
        game = replace(TEST_GAME, daily_pool=tuple([1] * days))
        obs = make_prompt_obs(day=days, today_history=[history()] if days > 1 else [])
        obs.game = game
        text = prompts.bid_request(obs)
        assert "本局最后一天，结束后金币清零。" in text
        assert "使用超级出价时，两笔出价不得同值" in text

    def test_earlier_day_has_no_final_day_reminder(self):
        text = prompts.bid_request(make_prompt_obs(day=1))
        assert "结束后金币清零" not in text
