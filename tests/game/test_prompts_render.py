"""局日志与排名的 markdown 表格渲染测试。共享工厂（make_day / make_log / make_standings）
见 conftest.py。"""

from dataclasses import replace

from auction_game.ai import prompts
from tests.support.factories import TEST_GAME, make_day, make_log, make_standings


class TestRenderMatchLog:
    def test_renders_markdown_table_with_player_columns(self):
        log = make_log(days=[
            make_day(1, [5, 4, 3, 2, 1, 0], 1, "甲", 5, [], 1),
            make_day(2, [3, 3, 2, 2, 2, 0], 1, "己", 0, [2, 3], 1),
        ])
        text = prompts.render_match_log(log)
        lines = text.splitlines()

        assert lines[0] == "| 天 | 奖池 | 甲 | 乙 | 丙 | 丁 | 戊 | 己 | 结果 | 撞车价位 |"
        assert lines[2] == "| 1 | 1 | 5（获胜） | 4（退款） | 3（退款） | 2（退款） | 1（退款） | 0（退款） | 甲 以 5 获胜 | — |"
        assert lines[3] == "| 2 | 1 | 3（撞车） | 3（撞车） | 2（撞车） | 2（撞车） | 2（撞车） | 0（获胜） | 己 以 0 获胜 | 2、3 |"
        assert "本局胜场：" in text

    def test_super_hit_and_miss_shown_in_cells(self):
        # 超级出价只展示引擎命中结果，不重复固定成本及悬赏。
        # （首日场景：乙超 6 命中甲的唯一 6，丙 5 捡漏获胜）
        log = make_log(days=[
            make_day(1, [6, 4, 5, 3, 3, 2], 1, "丙", 5, [3, 6], 1, super_bids={"乙": (6, True)}),
            make_day(2, [3, 4, 5, 3, 2, 1], 1, "丙", 5, [3], 1, super_bids={"甲": 6}),
        ])
        text = prompts.render_match_log(log)
        assert "6（撞车） | 4（退款）+超6（命中） | 5（获胜）" in text
        assert "3（撞车）+超6（落空） | 4（退款）" in text

    def test_empty_log(self):
        log = make_log(days=[])
        assert "未进行任何拍卖" in prompts.render_match_log(log)

    def test_custom_last_day_void_forfeits_pool(self):
        log = make_log(
            days=[
                make_day(2, [2, 2, 2, 1, 1, 1], 3, None, None, [1, 2], is_void=True),
                make_day(3, [2, 2, 2, 1, 1, 1], 6, None, None, [1, 2], is_void=True),
            ],
            game=replace(TEST_GAME, daily_pool=(2, 1, 3)),
        )
        text = prompts.render_match_log(log)
        assert "| 流局 | 1、2 |" in text
        assert "流局（奖池作废） | 1、2 |" in text
        assert "2（撞车）" in text and "1（撞车）" in text


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
