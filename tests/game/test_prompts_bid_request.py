"""每日出价消息（bid_request）：状态行、昨日公布、本人金币结算。"""

from dataclasses import replace

import pytest

from auction_game.ai import prompts
from tests.support.factories import (
    NAMES, TEST_GAME, make_day, make_logs, make_obs, make_standings, run_scenario, super_by_day,
)

BIDS = [4, 5, 3, 2, 1, 0]  # 乙以唯一最高的 5 获胜，其余退款


def make_prompt_obs(**kwargs):
    """bid_request 测试视角：当前是第 2 局。"""
    kwargs.setdefault("match_index", 2)
    return make_obs(**kwargs)


class TestBidRequest:
    """每日出价消息：首句「当前是」、昨日公布带获胜价与昨日出价明细（全透明：
    全场正常出价数值 + 各人超级出价数值）、状态行区分拍卖分与金币。"""

    def test_first_day_status_on_separate_lines(self):
        text = prompts.bid_request(make_prompt_obs(
            day=1, total_auction_score=3, total_score=17,
        ))
        first_line = text.splitlines()[0]
        assert "\n\n今日状态：\n\n- 今日奖池：1 分。\n- 你的金币余额：7（正常出价上限：7）。\n- 本局得分：0 分。\n\n" in text
        assert "昨日公布" not in text
        assert "已含今日工资" not in text  # 第 1 天没有工资
        assert first_line == "当前是第 2 局第 1 天。"
        assert "\n\n全员累计总分：甲 17 分、" in text

    @pytest.mark.parametrize("day", [1, 2])
    def test_live_totals_include_all_players(self, day):
        obs = make_prompt_obs(day=day, today_history=[make_day(1, BIDS)] if day > 1 else [])
        obs.standings = make_standings(
            {"甲": [3, 3], "乙": [2, 2]}, scores_history={"甲": [6, 6], "乙": [8, 9]},
        )
        assert "\n\n全员累计总分：乙 17 分、甲 12 分。" in prompts.bid_request(obs)

    @pytest.mark.parametrize("match_index,visible", [(1, False), (2, True), (3, True), (4, True)])
    def test_remaining_scores_only_final_regular_and_playoffs(self, match_index, visible):
        obs = make_prompt_obs(day=2, pool=3, today_history=[make_day(1, BIDS, pool=2)])
        obs.game = replace(TEST_GAME, regular_matches=2, daily_pool=(2, 3, 4))
        obs.match_index = match_index
        text = prompts.bid_request(obs)
        assert ("\n\n本局还剩 2 天（含今天），剩余拍卖奖池共 7 分。如果每天都命中，每人最多还能获得 4 命中分。" in text) == visible
        plan = prompts.next_match_plan_request(make_logs(match_index - 1, obs.game), [], obs.game)
        assert ("\n\n本局还剩 3 天（含今天），剩余拍卖奖池共 9 分。如果每天都命中，每人最多还能获得 6 命中分。" in plan) == visible
        assert "。。" not in plan
        assert plan.split("\n\n")[-2].endswith("。")

    def test_single_match_opening_and_final_day_remaining(self):
        game = replace(TEST_GAME, regular_matches=1, daily_pool=(2, 3))
        opening = prompts.opening_plan_request({}, game)
        assert "全员累计总分：全员 0 分。" in opening
        assert "\n\n本局还剩 2 天（含今天），剩余拍卖奖池共 5 分。如果每天都命中，每人最多还能获得 4 命中分。" in opening
        obs = make_prompt_obs(day=2, pool=3, today_history=[make_day(1, BIDS, pool=2)])
        obs.game = game
        text = prompts.bid_request(obs)
        assert "\n\n本局还剩 1 天（含今天），剩余拍卖奖池共 3 分。如果每天都命中，每人最多还能获得 2 命中分。" in text

    @pytest.mark.parametrize("daily_pool", [(2,), (2, 3, 4)])
    def test_first_day_remaining_scores_agree_across_three_requests(self, daily_pool):
        game = replace(
            TEST_GAME, regular_matches=1, daily_pool=daily_pool, super_hit_score=5,
        )
        obs = make_obs(game=game, pool=daily_pool[0])
        texts = [
            prompts.opening_plan_request({}, game),
            prompts.next_match_plan_request(make_logs(1, game), [], game),
            prompts.bid_request(obs),
        ]
        remaining_lines = [
            next(line for line in text.splitlines() if line.startswith("本局还剩"))
            for text in texts
        ]
        assert remaining_lines[0] == remaining_lines[1] == remaining_lines[2]
        assert f"剩余拍卖奖池共 {sum(daily_pool)} 分" in remaining_lines[0]
        assert f"本局还剩 {len(daily_pool)} 天（含今天）" in remaining_lines[0]
        assert f"如果每天都命中，每人最多还能获得 {len(daily_pool) * 5} 命中分" in remaining_lines[0]
        for text in texts[:2]:
            assert "今日奖池为 2 分" in text

    def test_max_affordable_bid_capped_by_balance(self):
        # 余额 4 低于上限时，可出最高价明示为 4
        text = prompts.bid_request(make_prompt_obs(day=1, coins=4))
        assert "- 你的金币余额：4（正常出价上限：4）。" in text.splitlines()

    def test_later_day_table_with_salary_note(self):
        text = prompts.bid_request(make_prompt_obs(
            day=2, today_history=[make_day(1, BIDS)], total_auction_score=5, match_score=7, total_score=23, coins=10,
            all_coins={"甲": 10, "乙": 8, "丙": 9, "丁": 7, "戊": 6, "己": 0},
        ))
        lines = text.splitlines()

        assert lines[0] == "当前是第 2 局第 2 天。"
        assert "昨日公布：乙 以 5 金币的正常出价获胜，拍卖得分 1 分。\n\n昨日无人命中。" in text
        assert "昨日出价明细与当前金币（已含今日工资）：\n\n" in text
        assert (
            "| 玩家 | 正常出价 | 正常出价结果 | 超级出价 | 命中情况 | 当前金币 |\n"
            "| --- | --- | --- | --- | --- | --- |\n"
            "| 甲 | 4 | 退款 | — | — | 10 |\n"
            "| 乙 | 5 | 获胜 | — | — | 8 |\n"
            "| 丙 | 3 | 退款 | — | — | 9 |\n"
            "| 丁 | 2 | 退款 | — | — | 7 |\n"
            "| 戊 | 1 | 退款 | — | — | 6 |\n"
            "| 己 | 0 | 退款 | — | — | 0 |\n\n"
        ) in text
        assert "- 今日奖池：1 分。\n- 你的金币余额：10（已含今日工资，正常出价上限：7）。\n- 本局得分：7 分。" in text
        assert "全员累计总分：甲 23 分、" in text
        # 余额行必须在昨日公布之后
        assert text.index("昨日公布") < text.index("你的金币余额")

    def test_yesterday_table_with_super_values_and_hits(self):
        # 昨日出价明细（全透明）：全场正常出价数值与结果 + 各人超级出价数值与命中标注。
        # 丙狙中甲的唯一 6，丁狙 7 落空，戊己互撞 2，乙以 5 获胜。
        text = prompts.bid_request(make_prompt_obs(
            day=2, today_history=[make_day(1, [6, 5, 4, 3, 2, 2], super_bids={"丙": 6, "丁": 7})],
        ))
        lines = text.splitlines()

        assert "| 甲 | 6 | 撞车 | — | — | 7 |" in lines  # 被狙中也算撞车
        assert "| 乙 | 5 | 获胜 | — | — | 7 |" in lines
        assert "| 丙 | 4 | 退款 | 6 | 命中 | 7 |" in lines
        assert "| 丁 | 3 | 退款 | 7 | 落空 | 7 |" in lines
        assert "| 戊 | 2 | 撞车 | — | — | 7 |" in lines
        assert "| 己 | 2 | 撞车 | — | — | 7 |" in lines
        assert "昨日命中得分：丙 2 分。" in lines

    def test_void_day_announcement_has_no_winner_wording(self):
        text = prompts.bid_request(make_prompt_obs(
            day=2,
            today_history=[make_day(1, [2, 2, 1, 1, 0, 0])],
            pool=1,
        ))
        lines = text.splitlines()

        assert "昨日公布：流局，奖池 1 分作废。" in lines
        status = next(line for line in lines if line.startswith("- 今日奖池："))
        assert "- 你的金币余额：7（已含今日工资，正常出价上限：7）。" in lines
        assert "今日奖池：1 分" in status


class TestMyLastDayLine:
    """本人金币结算保留正常出价扣款或退款、超级出价成本与悬赏。"""

    @pytest.mark.parametrize(
        "bids, super_bids, expected",
        [
            ([5, 4, 3, 2, 1, 0], {},
             "昨日你的金币结算：\n\n- 正常出价获胜，扣除 5 金币。"),
            ([4, 4, 3, 2, 1, 0], {},
             "昨日你的金币结算：\n\n- 正常出价撞车，扣除 4 金币。"),
            (BIDS, {},
             "昨日你的金币结算：\n\n- 正常出价没有撞车，但也没有获胜，金币全额退还。"),
            # 被超级出价狙中：按撞车扣款，与互撞分开表述
            ([6, 5, 4, 3, 2, 1], {"乙": 6},
             "昨日你的金币结算：\n\n- 正常出价被超级出价命中，扣除 6 金币。"),
            # 本来就互撞又被狙中：仍按被狙表述
            ([4, 4, 3, 2, 1, 0], {"丙": 4},
             "昨日你的金币结算：\n\n- 正常出价被超级出价命中，扣除 4 金币。"),
            ([1, 1, 0, 0, 2, 2], {},
             "昨日你的金币结算：\n\n- 所有正常出价都撞车，昨天流局，你的正常出价扣除 1 金币。"),
            # 超级出价落空：无效果，但照扣 2 金币成本；正常出价分段照常
            (BIDS, {"甲": 7},
             "昨日你的金币结算：\n\n- 正常出价没有撞车，但也没有获胜，金币全额退还。\n- 超级出价花费 2 金币。"),
            # 超级出价命中丙的唯一 3：扣 2 成本，获 3 金币悬赏
            (BIDS, {"甲": 3},
             "昨日你的金币结算：\n\n- 正常出价没有撞车，但也没有获胜，金币全额退还。\n- 超级出价花费 2 金币。\n- 获得 3 金币悬赏。"),
            ([4, 4, 3, 2, 1, 0], {"甲": 7},
             "昨日你的金币结算：\n\n- 正常出价撞车，扣除 4 金币。\n- 超级出价花费 2 金币。"),
        ],
        ids=["won", "collided", "refunded", "sniped", "collided-and-sniped", "void",
             "super-miss", "super-hit-bounty", "super-miss-collided"],
    )
    def test_last_day_line_segments(self, bids, super_bids, expected):
        text = prompts.bid_request(make_prompt_obs(
            day=2, today_history=[make_day(1, bids, super_bids=super_bids)],
        ))
        assert expected + "\n\n今日状态：\n\n- 今日奖池：" in text

    def test_line_position_between_announcement_and_status(self):
        text = prompts.bid_request(make_prompt_obs(
            day=2, today_history=[make_day(1, BIDS)],
        ))
        assert text.index("昨日公布") < text.index("昨日你的金币结算") < text.index("今日奖池")

    def test_custom_game_sets_daily_limit_cost_and_bounty(self):
        game = replace(TEST_GAME, max_bid=9, super_cost=3, super_bounty=5, super_hit_score=7)
        # 甲的超级出价 9 命中乙的唯一 9，丙以 5 获胜。
        obs = make_prompt_obs(
            day=2, coins=12, game=game,
            today_history=[make_day(1, [4, 9, 5, 2, 1, 0], super_bids={"甲": 9}, game=game)],
        )
        text = prompts.bid_request(obs)
        assert "正常出价上限：9" in text
        assert "- 超级出价花费 3 金币。\n- 获得 5 金币悬赏。" in text
        assert "昨日命中得分：甲 7 分。" in text

    @pytest.mark.parametrize("days", [1, 3])
    def test_final_day_reminds_coins_reset(self, days):
        game = replace(TEST_GAME, daily_pool=tuple([1] * days))
        obs = make_prompt_obs(day=days, today_history=[make_day(1, BIDS)] if days > 1 else [])
        obs.game = game
        text = prompts.bid_request(obs)
        assert "本局最后一天，结束后金币清零。" in text

    def test_earlier_day_has_no_final_day_reminder(self):
        text = prompts.bid_request(make_prompt_obs(day=1))
        assert "结束后金币清零" not in text


class TestEngineScorePrompts:
    """真实结算后的次日观察，验证提示与引擎分数一致。"""

    @pytest.mark.parametrize(
        "bids, super_bids, winner, scores, outcomes, personal_result",
        [
            ([4, 6, 2, 1, 0, 0], {"甲": 6}, "甲", [7, 0, 0, 0, 0, 0],
             ["获胜", "撞车", "退款", "退款", "撞车", "撞车"],
             "正常出价获胜，扣除 4 金币"),
            ([4, 4, 2, 2, 0, 0], {"戊": 4, "己": 4}, None, [0, 0, 0, 0, 5, 5],
             ["撞车"] * 6,
             "所有正常出价都撞车，昨天流局，你的正常出价扣除 0 金币"),
            ([4, 3, 2, 1, 0, 0], {"甲": 2, "乙": 4}, "乙", [5, 7, 0, 0, 0, 0],
             ["撞车", "获胜", "撞车", "退款", "撞车", "撞车"],
             "正常出价被超级出价命中，扣除 4 金币"),
        ],
        ids=["winner-also-hits", "void-multiple-hits-on-natural-collision", "sniped-also-hits"],
    )
    def test_yesterday_points_match_engine(self, bids, super_bids, winner, scores, outcomes, personal_result):
        game = replace(TEST_GAME, daily_pool=(2, 3), super_hit_score=5)
        captured = {}
        log = run_scenario(
            {1: bids},
            super_strategies={name: super_by_day({1: value}) for name, value in super_bids.items()},
            capture=captured, game=game,
        )
        expected_scores = dict(zip(NAMES, scores))
        assert log.days[0].winner == winner
        assert log.scores == expected_scores
        hits = "昨日命中得分：" + "、".join(
            f"{name} 5 分" for name in super_bids
        ) + "。"
        for name in NAMES:
            obs = captured[name][1]
            text = prompts.bid_request(obs)
            assert obs.my_match_score == obs.my_total_score == log.scores[name]
            assert hits in text
            assert f"本局得分：{log.scores[name]} 分。" in text
            total_line = next(line for line in text.splitlines() if line.startswith("全员累计总分："))
            for player, score in expected_scores.items():
                assert f"{player} {score} 分" in total_line
            for player, bid, outcome in zip(NAMES, bids, outcomes):
                row = next(line for line in text.splitlines() if line.startswith(f"| {player} |"))
                cells = [cell.strip() for cell in row.strip("|").split("|")]
                assert cells[1:3] == [str(bid), outcome]
                assert cells[-1] == str(obs.all_coins[player])
            if name in super_bids:
                index = NAMES.index(name)
                assert f"| {name} | {bids[index]} | {outcomes[index]} | {super_bids[name]} | 命中 |" in text
                assert "- 超级出价花费 2 金币。\n- 获得 3 金币悬赏。" in text
        observer = "戊" if winner is None else "甲"
        assert personal_result in prompts.bid_request(captured[observer][1])
        announcement = next(
            line for line in prompts.bid_request(captured[observer][1]).splitlines()
            if line.startswith("昨日公布：")
        )
        if winner is None:
            assert "昨日公布：流局，奖池 2 分作废。" in announcement
        else:
            assert f"昨日公布：{winner} 以 {log.days[0].winning_bid} 金币的正常出价获胜，拍卖得分 2 分。" in announcement

    @pytest.mark.parametrize("match_index", [1, 2], ids=["final-regular", "playoff"])
    def test_void_forfeits_pool_and_keeps_hit_points(self, match_index):
        game = replace(
            TEST_GAME, regular_matches=1, daily_pool=(2, 3, 4), super_hit_score=5,
        )
        captured = {}
        log = run_scenario(
            {1: [4, 4, 2, 2, 0, 0], 2: [4, 0, 0, 0, 0, 0]},
            super_strategies={name: super_by_day({1: 4}) for name in ["戊", "己"]},
            capture=captured, game=game,
        )
        assert log.auction_scores == dict(zip(NAMES, [3, 0, 0, 0, 0, 0]))
        assert log.scores == dict(zip(NAMES, [3, 0, 0, 0, 5, 5]))
        for name in ["戊", "己"]:
            day2, day3 = captured[name][1:]
            day2.match_index = day3.match_index = match_index
            assert day2.my_match_score == day3.my_match_score == log.scores[name] == 5
            after_void = prompts.bid_request(day2)
            assert "昨日公布：流局，奖池 2 分作废。" in after_void
            assert "昨日命中得分：戊 5 分、己 5 分。" in after_void
            assert "今日奖池：3 分" in after_void
            assert "\n\n本局还剩 2 天（含今天），剩余拍卖奖池共 7 分。如果每天都命中，每人最多还能获得 10 命中分。" in after_void
            assert "本局得分：5 分。" in after_void
            after_award = prompts.bid_request(day3)
            assert "昨日公布：甲 以 4 金币的正常出价获胜，拍卖得分 3 分。" in after_award
            assert "昨日无人命中。" in after_award
            assert "本局得分：5 分。" in after_award
            assert f"{name} 5 分" in after_award
            assert "\n\n本局还剩 1 天（含今天），剩余拍卖奖池共 4 分。如果每天都命中，每人最多还能获得 5 命中分。" in after_award
