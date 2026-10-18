"""局日志与排名的 markdown 表格渲染测试；共享工厂见 tests/support/factories.py。"""

from dataclasses import replace

import pytest

from auction_game.ai import prompts
from tests.support.factories import NAMES, TEST_GAME, make_day, make_log, make_standings


class TestRenderMatchLog:
    def test_renders_markdown_table_with_player_columns(self):
        log = make_log(
            days=[
                make_day(1, [5, 4, 3, 2, 1, 0], 1, "甲", 5, [], 1),
                make_day(2, [3, 3, 2, 2, 2, 0], 1, "己", 0, [2, 3], 1),
            ],
            auction_scores=dict(zip(NAMES, [1, 0, 0, 0, 0, 1])),
            scores=dict(zip(NAMES, [1, 0, 0, 0, 0, 1])),
        )
        text = prompts.render_match_log(log)
        lines = text.splitlines()

        assert lines[0] == "奖池和得分的单位为分，正常出价的单位为金币。"
        assert lines[1] == ""
        assert lines[2] == "| 天 | 奖池 | 甲 | 乙 | 丙 | 丁 | 戊 | 己 | 结果 | 撞车价位 | 当日得分 |"
        assert lines[4] == "| 1 | 1 | 5（获胜） | 4（退款） | 3（退款） | 2（退款） | 1（退款） | 0（退款） | 甲 以 5 金币获胜 | — | 甲 1 |"
        assert lines[5] == "| 2 | 1 | 3（撞车） | 3（撞车） | 2（撞车） | 2（撞车） | 2（撞车） | 0（获胜） | 己 以 0 金币获胜 | 2、3 | 己 1 |"
        assert "本局得分：甲 1 分、乙 0 分、丙 0 分、丁 0 分、戊 0 分、己 1 分" in text
        assert "本局拍卖分：甲 1 分、乙 0 分、丙 0 分、丁 0 分、戊 0 分、己 1 分" in text
        assert text.count("当日得分仅列非零玩家，未列者为 0 分。") == 1
        assert lines[6:9] == ["", "当日得分仅列非零玩家，未列者为 0 分。", ""]
        assert lines[10] == ""
        assert lines[9].startswith("本局得分：")
        assert lines[11].startswith("本局拍卖分：")

    def test_super_hit_and_miss_shown_in_cells(self):
        # 超级出价只展示引擎命中结果，不重复固定成本及悬赏。
        # （首日场景：乙超 6 命中甲的唯一 6，丙 5 捡漏获胜）
        log = make_log(days=[
            make_day(1, [6, 4, 5, 3, 3, 2], 1, "丙", 5, [3, 6], 1, super_bids={"乙": (6, True)}),
            make_day(2, [3, 4, 5, 3, 2, 1], 1, "丙", 5, [3], 1, super_bids={"甲": 6}),
        ], auction_scores=dict(zip(NAMES, [0, 0, 2, 0, 0, 0])),
            scores=dict(zip(NAMES, [0, 2, 2, 0, 0, 0])))
        text = prompts.render_match_log(log)
        assert "6（撞车） | 4（退款）；超级出价 6（命中） | 5（获胜）" in text
        assert "3（撞车）；超级出价 6（落空） | 4（退款）" in text
        assert "| 乙 2、丙 1 |" in text
        assert "本局得分：甲 0 分、乙 2 分、丙 2 分" in text
        rows = [line for line in text.splitlines() if line.startswith(("| 1 |", "| 2 |"))]
        for row in rows:
            player_cells = row.strip("|").split("|")[2:2 + len(NAMES)]
            assert all("金币" not in cell for cell in player_cells)
        assert "成本" not in text and "悬赏" not in text

    def test_empty_log(self):
        log = make_log(days=[])
        assert "未进行任何拍卖" in prompts.render_match_log(log)
        assert "本局得分：甲 0 分" in prompts.render_match_log(log)

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

    @pytest.mark.parametrize("hit_score, expected", [(3, 5), (0, 2)])
    def test_custom_hit_score_and_void_hits(self, hit_score, expected):
        game = replace(TEST_GAME, daily_pool=(1, 1), super_hit_score=hit_score)
        log = make_log(
            days=[
                make_day(1, [2, 2, 2, 1, 1, 1], 1, None, None, [1, 2], is_void=True,
                         super_bids={"甲": (1, True), "乙": (1, True)}),
                make_day(2, [5, 4, 3, 2, 1, 0], 2, "甲", 5, [4], 2,
                         super_bids={"甲": (4, True)}),
            ],
            auction_scores=dict(zip(NAMES, [2, 0, 0, 0, 0, 0])),
            scores=dict(zip(NAMES, [expected + hit_score, hit_score, 0, 0, 0, 0])),
            game=game,
        )
        text = prompts.render_match_log(log)
        void_scores = f"甲 {hit_score}、乙 {hit_score}" if hit_score else "—"
        assert f"| 流局 | 1、2 | {void_scores} |" in text
        assert f"| 甲 以 5 金币获胜 | 4 | 甲 {expected} |" in text
        assert text.count("当日得分仅列非零玩家，未列者为 0 分。") == 1
        assert f"本局得分：甲 {expected + hit_score} 分、乙 {hit_score} 分" in text

    def test_last_day_void_still_scores_each_hit(self):
        log = make_log(
            days=[make_day(1, [2, 2, 2, 1, 1, 1], 1, None, None, [1, 2], is_void=True,
                           super_bids={"甲": (1, True), "丁": (2, True)})],
            scores=dict(zip(NAMES, [2, 0, 0, 2, 0, 0])),
            game=replace(TEST_GAME, daily_pool=(1,)),
        )
        text = prompts.render_match_log(log)
        assert "流局（奖池作废） | 1、2 | 甲 2、丁 2 |" in text
        assert "本局得分：甲 2 分、乙 0 分、丙 0 分、丁 2 分" in text

    def test_summary_uses_settled_scores(self, monkeypatch):
        log = make_log(auction_scores=dict(zip(NAMES, [1, 0, 0, 0, 0, 0])),
                       scores=dict(zip(NAMES, [4, 0, 0, 0, 0, 0])))
        calls = []

        def day_scores(names, **kwargs):
            calls.append((names, kwargs))
            return dict.fromkeys(names, 0)

        monkeypatch.setattr(prompts, "calculate_day_scores", day_scores)
        text = prompts.render_match_log(log)
        assert calls == [(NAMES, {"winner": "甲", "auction_score": 1, "super_uses": [], "game": TEST_GAME})]
        assert text.splitlines()[4].endswith("| — |")
        assert "本局得分：甲 4 分" in text


class TestRenderStandings:
    def test_renders_markdown_table_with_match_columns(self):
        standings = make_standings({"甲": [2, 1], "乙": [1, 1], "丙": [0, 0]},
                                   scores_history={"甲": [8, 6], "乙": [4, 4], "丙": [2, 0]})
        text = prompts.render_standings(standings)
        lines = text.splitlines()

        assert lines[0] == "| 排名 | 玩家 | 总分 | 第1局 | 第2局 | 拍卖分 |"
        assert lines[2] == "| 1 | 甲 | 14 | 8 | 6 | 3 |"
        assert lines[4] == "| 3 | 丙 | 2 | 2 | 0 | 0 |"

    def test_tied_players_share_rank_cell(self):
        standings = make_standings({"甲": [2], "乙": [1]}, scores_history={"甲": [8], "乙": [8]})
        text = prompts.render_standings(standings)
        assert "| 1 | 甲 | 8 | 8 | 2 |" in text
        assert "| 1 | 乙 | 8 | 8 | 1 |" in text

    def test_less_auction_score_can_lead_by_total_score(self):
        standings = make_standings({"甲": [2], "乙": [1]}, scores_history={"甲": [8], "乙": [9]})
        lines = prompts.render_standings(standings).splitlines()
        assert lines[2] == "| 1 | 乙 | 9 | 9 | 1 |"
        assert lines[3] == "| 2 | 甲 | 8 | 8 | 2 |"

    def test_match_columns_follow_score_history(self):
        standings = make_standings({"甲": [2]}, scores_history={"甲": [4, 6]}, total_scores={"甲": 13})
        text = prompts.render_standings(standings)
        assert "| 第2局 |" in text
        assert "| 1 | 甲 | 13 | 4 | 6 | 2 |" in text

    def test_no_match_columns_when_history_empty(self):
        standings = make_standings({"甲": [], "乙": []})
        text = prompts.render_standings(standings)
        assert text.splitlines()[0] == "| 排名 | 玩家 | 总分 | 拍卖分 |"
