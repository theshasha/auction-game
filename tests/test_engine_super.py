"""超级出价（v5 狙击悬赏制）的引擎测试。场景构造器见 conftest.py。

v5 要点：每局 1 次机会、命中即恢复（无上限）；0~6 不查余额、不能等于
当天正常出价、不能获胜、永不扣款；命中 = 其数值上恰好 1 笔正常出价 →
该正常出价按撞车处理，狙击者获得系统印发的 3 金币悬赏。
"""

from conftest import run_scenario, super_by_day


class TestSuperBid:
    def test_snipe_unique_normal_bounty_and_restore(self):
        # RULES.md 结算示例一：甲出 6；乙正常出 4 + 超级出价 6；丙 5；丁戊 3；己 2。
        # 乙的超级 6 命中甲的唯一 6（甲按撞车扣 6），3 互撞（丁戊各扣 3），
        # 丙未撞车最高 5 获胜扣 5；乙 +3 悬赏且机会恢复，其正常出价 4 退还
        log = run_scenario(
            {1: [6, 4, 5, 3, 3, 2], 2: [6, 6, 0, 0, 0, 0]},
            super_strategies={"乙": super_by_day({1: 6, 2: 5})},
        )
        day1 = log.days[0]
        assert day1.winner == "丙" and day1.winning_bid == 5
        assert day1.super_uses == [{"player": "乙", "value": 6, "hit": True}]
        assert day1.collision_values == [3, 6]  # 甲被狙的 6 + 丁戊互撞的 3

        records = {r.player: r for r in log.days[1].bids if not r.is_super}
        # 甲被狙扣 6：余额 0+3=3，出 6 应非法
        assert records["甲"].bid == 0 and records["甲"].status == "illegal"
        # 乙第 1 天分文未扣还拿 3 悬赏：余额 6+3+3=12，出 6 合法
        assert records["乙"].bid == 6 and records["乙"].status == "ok"
        # 命中即恢复：第 2 天乙的超级出价（5，异于正常出价 6）再次被受理
        day2_super = next(
            (r for r in log.days[1].bids if r.player == "乙" and r.is_super), None
        )
        assert day2_super is not None and day2_super.bid == 5

    def test_super_whiff_consumed_when_no_normal_at_value(self):
        # 乙正常出 4 + 超级出价 6：正常出价 5/4/3/2/1/0 全唯一，6 上没有正常出价
        # → 落空：机会消耗、无效果；甲的唯一 5 获胜；乙分文未动但机会没了
        log = run_scenario(
            {1: [5, 4, 3, 2, 1, 0], 2: [0, 6, 0, 0, 0, 0]},
            super_strategies={"乙": lambda obs: 6},  # 每天都试图用
        )
        day1 = log.days[0]
        assert day1.winner == "甲" and day1.winning_bid == 5
        assert day1.super_uses == [{"player": "乙", "value": 6, "hit": False}]
        assert day1.collision_values == []

        record_yi = next(
            r for r in log.days[1].bids if r.player == "乙" and not r.is_super
        )
        assert record_yi.bid == 6 and record_yi.status == "ok"  # 余额 9：分文未扣
        # 落空即消耗：第 2 天的超级出价提交被引擎丢弃（不进日志）
        assert log.days[1].super_uses == []
        assert not any(r.is_super for r in log.days[1].bids if r.player == "乙")

    def test_super_bid_ignores_balance(self):
        # 甲第 1 天出 6 获胜余额归零；第 2 天 +3 后正常出价最多出 3，
        # 但超级出价不受余额限制可以出 6
        log = run_scenario(
            {1: [6, 1, 1, 1, 1, 1]},
            super_strategies={"甲": super_by_day({2: 6})},
        )
        day2 = log.days[1]
        assert day2.super_uses == [{"player": "甲", "value": 6, "hit": False}]
        # 第 2 天所有人正常出 0（六笔 0 互撞），甲的超级 6 上没有正常出价 → 落空；
        # 正常出价全灭：当天流局，奖池并入第 3 天
        assert day2.is_void and day2.winner is None
        assert log.days[2].pool == 2

    def test_super_bid_equal_to_normal_is_dropped(self):
        # 超级出价不能等于当天正常出价：同值提交被丢弃（视为未使用），机会保留
        log = run_scenario(
            {1: [5, 4, 3, 2, 1, 0]},
            super_strategies={"甲": super_by_day({1: 5})},
        )
        day1 = log.days[0]
        assert day1.super_uses == []  # 同值非法，不进日志
        assert not any(r.is_super for r in day1.bids)
        assert day1.winner == "甲" and day1.winning_bid == 5  # 5 唯一获胜
        assert day1.collision_values == []

        # 机会未被消耗：第 2 天甲的超级出价状态仍可用
        captured = {}
        run_scenario(
            {1: [5, 4, 3, 2, 1, 0]},
            super_strategies={"甲": super_by_day({1: 5})},
            capture=captured,
        )
        assert captured["甲"][1].my_super_available

    def test_multi_snipers_same_value_both_rewarded(self):
        # 多人命中同一数值各得 3 金币：乙、己的超级 6 同时命中甲的唯一 6
        log = run_scenario(
            {1: [6, 4, 5, 3, 3, 2], 2: [6, 6, 0, 0, 0, 6]},
            super_strategies={"乙": super_by_day({1: 6}), "己": super_by_day({1: 6})},
        )
        day1 = log.days[0]
        assert day1.super_uses == [
            {"player": "乙", "value": 6, "hit": True},
            {"player": "己", "value": 6, "hit": True},
        ]
        assert day1.winner == "丙" and day1.winning_bid == 5
        records = {r.player: r for r in log.days[1].bids if not r.is_super}
        # 乙/己各获 3 悬赏：余额 6+3+3=12，出 6 合法；甲被狙扣 6：余额 3，出 6 非法
        assert records["乙"].status == "ok" and records["己"].status == "ok"
        assert records["甲"].status == "illegal"

    def test_snipe_non_top_unique_also_pays_bounty(self):
        # 命中不限于最高唯一价：乙的超级 0 命中己的唯一 0（扣 0 无实质损失），
        # 乙照拿 3 金币悬赏；胜者仍是未撞车最高的甲
        log = run_scenario(
            {1: [5, 4, 3, 2, 1, 0]},
            super_strategies={"乙": super_by_day({1: 0})},
        )
        day1 = log.days[0]
        assert day1.super_uses == [{"player": "乙", "value": 0, "hit": True}]
        assert day1.winner == "甲" and day1.winning_bid == 5
        assert day1.collision_values == [0]

    def test_super_observation_fields(self):
        # Observation 携带超级出价状态：可用/已用（含使用日）、昨日超级出价结局；
        # 命中后机会恢复（第 2 天仍可用）；公开视图全透明（数值 + 是否命中）
        captured = {}
        run_scenario(
            {1: [6, 4, 5, 3, 3, 2]},
            super_strategies={"乙": super_by_day({1: 6})},
            capture=captured,
        )
        day1_obs = captured["乙"][0]
        assert day1_obs.my_super_available and day1_obs.my_super_used_day is None
        day2_obs = captured["乙"][1]
        # 命中即恢复：第 2 天机会仍可用；used_day 记录最近一次使用日
        assert day2_obs.my_super_available
        assert day2_obs.my_super_used_day == 1
        assert day2_obs.my_last_super_bid == 6
        assert day2_obs.my_last_super_hit  # 命中甲的唯一 6
        # 被狙者视角：甲的正常出价 6 标记为「被狙」，按撞车扣 6
        assert captured["甲"][1].my_last_sniped
        assert captured["甲"][1].my_last_collided
        assert captured["甲"][1].my_last_deduction == 6
        # 未使用者的昨日字段为默认值
        assert captured["丙"][1].my_last_super_bid is None
        assert captured["丙"][1].my_super_available
        # 每日公布全透明：谁用了、数值几、是否命中
        assert captured["丙"][1].today_history[0]["super_uses"] == [
            {"player": "乙", "value": 6, "hit": True}
        ]
        assert captured["丙"][1].today_history[0]["collision_values"] == [3, 6]
