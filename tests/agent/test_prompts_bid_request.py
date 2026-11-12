"""每日出价消息（bid_request）：状态行、昨日明细、流局和只狙到队友的说明。"""

from dataclasses import replace

import pytest

from auction_game.agent import prompts
from tests.support.factories import (
    NAMES, TEAM_OF, TEST_GAME, make_day, make_logs, make_obs, make_standings, run_scenario, super_by_day,
)

BIDS = [4, 5, 3, 2, 1, 0]  # 乙以唯一最高的 5 获胜，其余退款
YESTERDAY = "昨日出价明细："
COINS = "当前金币（已含今日工资）："


def table_rows(text, heading):
    """heading 下面那张表的数据行，每行是去掉首尾空白的单元格。"""
    block = text.split(f"{heading}\n\n", 1)[1].split("\n\n", 1)[0]
    return [[cell.strip() for cell in line.strip("|").split("|")] for line in block.splitlines()[2:]]


def make_prompt_obs(**kwargs):
    """bid_request 测试视角：当前是第 2 局。"""
    kwargs.setdefault("match_index", 2)
    return make_obs(**kwargs)


class TestBidRequest:
    """每日出价消息：首句「当前是」、按价位排的昨日出价明细、当前金币、状态行区分拍卖分与金币。"""

    def test_live_totals_include_all_players(self):
        obs = make_prompt_obs(day=2, today_history=[make_day(1, BIDS)])
        obs.standings = make_standings(
            {"甲": [3, 3], "乙": [2, 2]}, scores_history={"甲": [6, 6], "乙": [8, 9]},
        )
        assert "\n\n全员累计总分：乙 17 分、甲 12 分。" in prompts.bid_request(obs, team=False)

    @pytest.mark.parametrize("match_index,visible", [(1, False), (2, True), (3, True), (4, True)])
    def test_remaining_scores_only_final_regular_and_playoffs(self, match_index, visible):
        obs = make_prompt_obs(day=2, pool=3, today_history=[make_day(1, BIDS, pool=2)])
        obs.game = replace(TEST_GAME, regular_matches=2, daily_pool=(2, 3, 4))
        obs.match_index = match_index
        text = prompts.bid_request(obs, team=False)
        assert ("\n\n本局还剩 2 天（含今天），剩余拍卖奖池共 7 分。如果每天都命中，每人最多还能获得 4 命中分。" in text) == visible
        tied = make_standings({"甲": [0], "乙": [0]})
        plan = prompts.next_match_plan_request(make_logs(match_index - 1, obs.game), tied, obs.game)
        assert ("\n\n本局还剩 3 天（含今天），剩余拍卖奖池共 9 分。如果每天都命中，每人最多还能获得 6 命中分。" in plan) == visible
        assert "。。" not in plan
        assert plan.split("\n\n")[-2].endswith("。")

    def test_single_match_opening_and_final_day_remaining(self):
        game = replace(TEST_GAME, regular_matches=1, daily_pool=(2, 3))
        opening = prompts.opening_plan_request({}, game, team=False)
        assert "\n\n本局还剩 2 天（含今天），剩余拍卖奖池共 5 分。如果每天都命中，每人最多还能获得 4 命中分。" in opening
        obs = make_prompt_obs(day=2, pool=3, today_history=[make_day(1, BIDS, pool=2)])
        obs.game = game
        text = prompts.bid_request(obs, team=False)
        assert "\n\n本局还剩 1 天（含今天），剩余拍卖奖池共 3 分。如果每天都命中，每人最多还能获得 2 命中分。" in text

    @pytest.mark.parametrize("daily_pool", [(2,), (2, 3, 4)])
    def test_first_day_remaining_scores_agree_across_plan_requests(self, daily_pool):
        game = replace(
            TEST_GAME, regular_matches=1, daily_pool=daily_pool, super_hit_score=5,
        )
        texts = [
            prompts.opening_plan_request({}, game, team=False),
            prompts.next_match_plan_request(make_logs(1, game), make_standings({"甲": [0], "乙": [0]}), game),
        ]
        remaining_lines = [
            next(line for line in text.splitlines() if line.startswith("本局还剩"))
            for text in texts
        ]
        assert remaining_lines[0] == remaining_lines[1]
        assert f"剩余拍卖奖池共 {sum(daily_pool)} 分" in remaining_lines[0]
        assert f"本局还剩 {len(daily_pool)} 天（含今天）" in remaining_lines[0]
        assert f"如果每天都命中，每人最多还能获得 {len(daily_pool) * 5} 命中分" in remaining_lines[0]
        for text in texts:
            assert "今日奖池为 2 分" in text

    def test_max_affordable_bid_capped_by_balance(self):
        # 余额 4 低于上限时，可出最高价明示为 4
        text = prompts.bid_request(make_prompt_obs(day=2, coins=4, today_history=[make_day(1, BIDS)]), team=False)
        assert "- 你的金币余额：4（正常出价上限：4）。" in text.splitlines()

    def test_later_day_table_with_salary_note(self):
        text = prompts.bid_request(make_prompt_obs(
            day=2, today_history=[make_day(1, BIDS)], total_auction_score=5, match_score=7, total_score=23, coins=10,
            all_coins={"甲": 10, "乙": 8, "丙": 9, "丁": 7, "戊": 6, "己": 0},
        ), team=False)
        lines = text.splitlines()

        assert lines[0] == "当前是第 2 局第 2 天。"
        assert lines[2] == YESTERDAY
        assert (
            "| 价位 | 正常出价 | 结果 | 超级出价 |\n"
            "| --- | --- | --- | --- |\n"
            "| 5 | 乙 | 获胜 | — |\n"
            "| 4 | 甲 | 退款 | — |\n"
            "| 3 | 丙 | 退款 | — |\n"
            "| 2 | 丁 | 退款 | — |\n"
            "| 1 | 戊 | 退款 | — |\n"
            "| 0 | 己 | 退款 | — |\n\n"
            f"{COINS}\n\n"
            "| 玩家 | 当前金币 |\n"
            "| --- | --- |\n"
            "| 甲 | 10 |\n"
            "| 乙 | 8 |\n"
            "| 丙 | 9 |\n"
            "| 丁 | 7 |\n"
            "| 戊 | 6 |\n"
            "| 己 | 0 |\n\n"
        ) in text
        assert "- 今日奖池：1 分。\n- 你的金币余额：10（正常出价上限：7）。\n- 本局得分：7 分。" in text
        assert "全员累计总分：甲 23 分、" in text
        assert text.index(COINS) < text.index("你的金币余额")

    def test_yesterday_table_with_super_values_and_hits(self):
        # 丙狙中甲独占的 6，丁狙没人出的 7 落空，戊己互撞 2，乙以 5 获胜；没人出的 1 不列。
        text = prompts.bid_request(make_prompt_obs(
            day=2, today_history=[make_day(1, [6, 5, 4, 3, 2, 2], super_bids={"丙": 6, "丁": 7})],
        ), team=False)

        assert table_rows(text, YESTERDAY) == [
            ["7", "—", "—", "丁（落空）"],
            ["6", "甲", "撞车扣款", "丙（命中）"],  # 被狙中也算撞车
            ["5", "乙", "获胜", "—"],
            ["4", "丙", "退款", "—"],
            ["3", "丁", "退款", "—"],
            ["2", "戊、己", "撞车扣款", "—"],
        ]

    def test_void_day_says_pool_is_forfeited(self):
        text = prompts.bid_request(make_prompt_obs(
            day=2,
            today_history=[make_day(1, [2, 2, 1, 1, 0, 0])],
            pool=1,
        ), team=False)
        lines = text.splitlines()

        assert lines[2] == "昨日流局，奖池 1 分作废。"
        status = next(line for line in lines if line.startswith("- 今日奖池："))
        assert "- 你的金币余额：7（正常出价上限：7）。" in lines
        assert "今日奖池：1 分" in status

    def test_day_with_winner_has_no_void_line(self):
        text = prompts.bid_request(make_prompt_obs(day=2, today_history=[make_day(1, BIDS)]), team=False)
        assert "流局" not in text

    def test_custom_max_bid_sets_daily_limit(self):
        game = replace(TEST_GAME, max_bid=9)
        obs = make_prompt_obs(day=2, coins=12, game=game, today_history=[make_day(1, BIDS, game=game)])
        assert "正常出价上限：9" in prompts.bid_request(obs, team=False)

    @pytest.mark.parametrize("days", [2, 3])
    def test_final_day_reminds_coins_reset(self, days):
        game = replace(TEST_GAME, daily_pool=tuple([1] * days))
        obs = make_prompt_obs(day=days, today_history=[make_day(1, BIDS)])
        obs.game = game
        text = prompts.bid_request(obs, team=False)
        assert "本局最后一天，结束后金币清零。" in text

    def test_earlier_day_has_no_final_day_reminder(self):
        text = prompts.bid_request(make_prompt_obs(day=2, today_history=[make_day(1, BIDS)]), team=False)
        assert "结束后金币清零" not in text


def team_obs(bids, super_bids=None):
    """甲视角的团队赛出价请求依据：昨天按 TEAM_OF 结算，累计蓝队 6 分、红队 4 分。"""
    obs = make_prompt_obs(day=2, today_history=[make_day(1, bids, super_bids=super_bids, team_of=TEAM_OF)])
    obs.standings = make_standings({name: [0] for name in NAMES},
                                   scores_history=dict(zip(NAMES, [[3], [1], [0], [4], [2], [0]])), team_of=TEAM_OF)
    return obs


class TestTeamBidRequest:
    """团队赛：累计总分按阵营分组，金币表多一列阵营，只狙到队友价位的落空写明原因。TEAM_OF 里甲乙丙是红队。"""

    def test_totals_grouped_by_team_and_coins_table_has_team_column(self):
        text = prompts.bid_request(team_obs(BIDS), team=True)
        assert "\n\n累计总分：蓝队 6 分（丁 4 分、戊 2 分、己 0 分），红队 4 分（甲 3 分、乙 1 分、丙 0 分）。" in text
        assert "全员累计总分" not in text
        assert f"{COINS}\n\n| 玩家 | 阵营 | 当前金币 |\n" in text
        assert table_rows(text, COINS)[0] == ["甲", "红队", "7"]
        assert table_rows(text, COINS)[3] == ["丁", "蓝队", "7"]
        assert ["4", "甲", "退款", "—"] in table_rows(text, YESTERDAY)

    def test_same_value_lists_hits_before_misses(self):
        # 乙独占 5：对方的丁命中，乙的队友甲落空。
        text = prompts.bid_request(team_obs([0, 5, 1, 2, 3, 4], {"甲": 5, "丁": 5}), team=True)
        assert ["5", "乙", "撞车扣款", "丁（命中）；甲（落空）"] in table_rows(text, YESTERDAY)

    @pytest.mark.parametrize("bids, super_bid, note", [
        ([0, 5, 1, 2, 3, 4], 5, "你的超级出价落空：5 上只有你的队友乙。"),
        ([0, 5, 5, 2, 3, 4], 5, "你的超级出价落空：5 上只有你的队友乙、丙。"),
        ([0, 5, 1, 2, 3, 4], 6, None),
        ([0, 5, 1, 2, 3, 4], 2, None),
    ], ids=["one-teammate", "two-teammates", "nobody-there", "hit-opponent"])
    def test_miss_on_teammates_only_value_names_them(self, bids, super_bid, note):
        text = prompts.bid_request(team_obs(bids, {"甲": super_bid}), team=True)
        if note is None:
            assert "你的超级出价落空" not in text
        else:
            assert f"|\n\n{note}\n\n{COINS}" in text


class TestEngineScorePrompts:
    """真实结算后的次日观察，验证提示与引擎分数一致。"""

    @pytest.mark.parametrize(
        "bids, super_bids, winner, scores, outcomes",
        [
            ([4, 6, 2, 1, 0, 0], {"甲": 6}, "甲", [7, 0, 0, 0, 0, 0],
             ["获胜", "撞车扣款", "退款", "退款", "撞车扣款", "撞车扣款"]),
            ([4, 4, 2, 2, 0, 0], {"戊": 4, "己": 4}, None, [0, 0, 0, 0, 5, 5],
             ["撞车扣款"] * 6),
            ([4, 3, 2, 1, 0, 0], {"甲": 2, "乙": 4}, "乙", [5, 7, 0, 0, 0, 0],
             ["撞车扣款", "获胜", "撞车扣款", "退款", "撞车扣款", "撞车扣款"]),
        ],
        ids=["winner-also-hits", "void-multiple-hits-on-natural-collision", "sniped-also-hits"],
    )
    def test_yesterday_points_match_engine(self, bids, super_bids, winner, scores, outcomes):
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
        for name in NAMES:
            obs = captured[name][1]
            text = prompts.bid_request(obs, team=False)
            assert obs.my_match_score == log.scores[name]
            assert f"本局得分：{log.scores[name]} 分。" in text
            total_line = next(line for line in text.splitlines() if line.startswith("全员累计总分："))
            for player, score in expected_scores.items():
                assert f"{player} {score} 分" in total_line
            by_value = {int(row[0]): row for row in table_rows(text, YESTERDAY)}
            for player, bid, outcome in zip(NAMES, bids, outcomes):
                assert player in by_value[bid][1].split("、")
                assert by_value[bid][2] == outcome
            assert {row[0]: row[-1] for row in table_rows(text, COINS)} == {
                player: str(obs.all_coins[player]) for player in NAMES}
            for player, value in super_bids.items():
                hit_group = next(group for group in by_value[value][3].split("；") if group.endswith("（命中）"))
                assert player in hit_group.removesuffix("（命中）").split("、")

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
            after_void = prompts.bid_request(day2, team=False)
            assert "昨日流局，奖池 2 分作废。" in after_void
            assert "今日奖池：3 分" in after_void
            assert "\n\n本局还剩 2 天（含今天），剩余拍卖奖池共 7 分。如果每天都命中，每人最多还能获得 10 命中分。" in after_void
            assert "本局得分：5 分。" in after_void
            after_award = prompts.bid_request(day3, team=False)
            assert "本局得分：5 分。" in after_award
            assert f"{name} 5 分" in after_award
            assert "\n\n本局还剩 1 天（含今天），剩余拍卖奖池共 4 分。如果每天都命中，每人最多还能获得 5 命中分。" in after_award
