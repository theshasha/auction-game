"""提示词文案：system 提示词、局末结果、记忆、收到的私信、加赛预告、规划与首日出价合并。
每日出价消息见 test_prompts_bid_request.py；表格渲染见 tests/game/test_tables.py；
规则文本见 tests/game/test_rules.py。"""

from dataclasses import replace

import pytest

from auction_game.agent import prompts
from auction_game.game import tables
from tests.support.factories import (NAMES, TEST_GAME, make_day, make_lineup, make_log, make_logs, make_obs,
                                     make_standings)


class TestSystemPrompt:
    """system 提示词：规则全文 + 身份 + 人设 + 全员公开的赛前预测 + 人设作用范围。"""

    def test_includes_persona_and_scope(self):
        lineup = make_lineup(persona=("傲娇大小姐", "自称本小姐。"))
        system = prompts.build_system_prompt(lineup.profiles[0], lineup, TEST_GAME)
        assert "你叫 甲" in system
        assert "傲娇大小姐：自称本小姐。" in system
        # 人设只影响说话方式，思考要求保持认真
        assert "不要被人设的说话方式影响你的思考" in system
        assert "发言和私信中的承诺都不强制履行" in system

    def test_predictions_public_to_everyone(self):
        lineup = make_lineup(rates=[50, 20, 10, 10, 10, 0])
        for profile in lineup.profiles:
            system = prompts.build_system_prompt(profile, lineup, TEST_GAME)
            assert "甲 50%" in system and "己 0%" in system  # 全员可见，含他人与自己的
        system = prompts.build_system_prompt(lineup.profiles[0], lineup, TEST_GAME)
        assert "乙、丙、丁、戊、己" in system  # 其他五位玩家名单

    @pytest.mark.parametrize("enable_predictions", [False, True])
    @pytest.mark.parametrize("enable_personas", [False, True])
    def test_independent_switches_remove_entire_sections(self, enable_predictions, enable_personas):
        from auction_game.game.rules import render_rules

        lineup = make_lineup(rates=[10, 73, 10, 10, 10, 10], persona=("独特人设标题", "独特人设正文"))
        text = prompts.build_system_prompt(
            lineup.profiles[0], lineup, TEST_GAME,
            enable_predictions=enable_predictions, enable_personas=enable_personas,
        )
        for marker in ("【赛前预测】", "专家", "夺冠率预测", "乙 73%"):
            assert (marker in text) is enable_predictions
        for marker in ("【你的人设】", "独特人设标题", "独特人设正文", "人设", "认真思考"):
            assert (marker in text) is enable_personas
        assert "你叫 甲" in text and "乙、丙、丁、戊、己" in text
        assert text.startswith(render_rules(TEST_GAME, lineup, include_note=False))

    @pytest.mark.parametrize("enable_personas", [False, True])
    @pytest.mark.parametrize("writing_style", [None, "【写作要求】\n独特写作要求。\n"])
    def test_writing_style_is_appended_last(self, enable_personas, writing_style):
        lineup = make_lineup()
        text = prompts.build_system_prompt(
            lineup.profiles[0], lineup, TEST_GAME,
            enable_personas=enable_personas, writing_style=writing_style,
        )
        if writing_style is None:
            assert "【写作要求】" not in text
        else:
            assert text.endswith("\n" + writing_style)


class TestSpeechFields:
    """局末结果和观众总结。"""

    @pytest.mark.parametrize("final", [False, True], ids=["match-end", "final"])
    def test_results_show_settled_scores_and_score_based_ranking(self, final):
        log = make_log(days=[make_day(1, [5, 4, 3, 2, 1, 0], pool=3)])
        standings = make_standings({"甲": [2, 1], "乙": [1, 0]},
                                   scores_history={"甲": [8, 4], "乙": [12, 2]})
        text = (prompts.final_speech_request(standings, log) if final
                else prompts.match_end_speech_request(log, standings))
        assert "本局得分：甲 3 分、乙 0 分" in text
        assert "排名与分数明细" in text
        assert f"本局完整出价日志：\n\n{tables.render_match_log(log)}" in text
        assert f"排名与分数明细：\n\n{tables.render_standings(standings)}" in text
        assert "| 1 | 乙 | 14 | 12 | 2 | 1 |" in text
        assert "| 2 | 甲 | 12 | 8 | 4 | 3 |" in text

    def test_final_summary_is_private_and_includes_speeches(self):
        text = prompts.final_summary_request({"甲": "感言A", "乙": "感言B"})
        assert text.startswith("以下是其他玩家的最终感言：\n\n【甲】感言A\n\n【乙】感言B\n\n最后")
        assert "只给观众看的总结发言" in text
        assert "用第一人称" in text


@pytest.mark.parametrize("match_index", [1, 2])
def test_memory_request_lists_other_speeches_and_key_requirements(match_index):
    text = prompts.memory_request({"乙": "公开信号", "丙": "另一条公开信号"}, match_index)
    assert "公开发言：\n\n【乙】公开信号\n\n【丙】另一条公开信号\n\n请结合" in text
    # 没有收到信也提私信：本局出价时收到的信在对话里。
    for required in ("公开发言和私信，整理你的记忆", "看不到之前各局的原始对话、公开发言和私信",
                     "私信里谈好的约定和得到的消息", "只写结论", "不超过 1000 字"):
        assert required in text
    # 第 1 局结束时还没有旧记忆，之后每局都要在旧记忆上更新。
    for old_memory in ("结合旧记忆", "新记忆会完整替换旧记忆", "保留仍然有用的旧经验", "改掉被本局推翻的判断"):
        assert (old_memory in text) is (match_index > 1)


@pytest.mark.parametrize("kind", ["opening-plan", "bid", "match-end", "playoff-match-end", "memory", "final"])
def test_received_letters_sit_between_facts_and_task(kind):
    """有信时只多出收信段，标题写明信是在哪一环节写的；没有信时请求不变。"""
    log = make_log()
    playoff_log = make_log(match_index=TEST_GAME.regular_matches)
    standings = make_standings({"甲": [1], "乙": [0]})
    obs = make_obs(day=2, today_history=[make_day(1, [5, 4, 3, 2, 1, 0])])
    build, fact, written, task = {
        "opening-plan": (lambda received: prompts.opening_plan_request({"乙": "开局发言"}, TEST_GAME,
                                                                       received=received),
                         "【乙】开局发言", "在开局发言时", "请结合他们的发言"),
        "bid": (lambda received: prompts.bid_request(obs, received=received), "全员累计总分：", "昨天出价时",
                prompts.BID_HINT),
        "match-end": (lambda received: prompts.match_end_speech_request(log, standings, received=received),
                      tables.render_standings(standings), "在本局最后一天出价时", "请向全体玩家发表你的局末发言"),
        "playoff-match-end": (lambda received: prompts.match_end_speech_request(playoff_log, standings,
                                                                                received=received),
                              "第一名总分并列，全员进入加赛。", "在本局最后一天出价时", "请向全体玩家发表你的局末发言"),
        "memory": (lambda received: prompts.memory_request({"乙": "局末发言"}, 2, received=received),
                   "【乙】局末发言", "在局末发言时", "请结合旧记忆"),
        "final": (lambda received: prompts.final_speech_request(standings, log, received=received),
                  tables.render_standings(standings), "在本局最后一天出价时", "请向全体玩家发表你的最终感言"),
    }[kind]
    block = f"以下是其他玩家{written}写给你的私信：\n\n【乙】第一封\n第二行\n\n【丙】第二封"
    text = build({"乙": "第一封\n第二行", "丙": "第二封"})
    assert f"{block}\n\n{task}" in text
    assert text.index(fact) < text.index(block)
    assert text.replace(f"{block}\n\n", "") == build(None) == build({})
    assert "写给你的私信" not in build(None)


class TestPlayoffNotice:
    """局末发言只在还有下一局时发出；正赛打完后还有下一局就预告加赛，加赛局的规划请求写明并列第一的玩家。"""

    @pytest.mark.parametrize(
        "regular_matches, match_index, is_playoff, expected",
        [
            (5, 2, False, False),
            (5, 5, False, True),
            (5, 6, True, True),
            (2, 2, False, True),
        ],
        ids=["early", "regular-tied", "playoff-tied", "custom-tied"],
    )
    def test_notice_at_regular_and_playoff_boundaries(
        self, regular_matches, match_index, is_playoff, expected,
    ):
        text = prompts.match_end_speech_request(
            make_log(
                match_index=match_index, is_playoff=is_playoff,
                game=replace(TEST_GAME, regular_matches=regular_matches),
            ),
            make_standings(
                {name: [int(name == "甲")] * match_index for name in NAMES},
                scores_history={name: [4 if name in ("甲", "乙") else 0] * match_index for name in NAMES},
            ),
        )
        notice = "第一名总分并列，全员进入加赛。"
        assert ("第一名总分并列" in text) is expected
        if expected:
            assert notice in text

    @pytest.mark.parametrize("match_index", [3, 4, 5], ids=["last-regular", "first-playoff", "next-playoff"])
    def test_playoff_plan_names_tied_leaders(self, match_index):
        game = replace(TEST_GAME, regular_matches=3)
        standings = make_standings({"甲": [1], "乙": [2], "丙": [0]}, scores_history={"甲": [9], "乙": [9], "丙": [4]})
        text = prompts.next_match_plan_request(make_logs(match_index - 1, game), standings, game)
        notice = f"第 {match_index} 局是加赛局。目前第一名总分并列：乙、甲各 9 分。\n\n之前各局的完整出价日志"
        assert text.startswith(notice) is (match_index > game.regular_matches)


class TestPlanBidMerge:
    """规划请求保留首日资源、累计分数与策略要求。"""

    def test_single_day_plan_reminds_coins_reset(self):
        game = replace(TEST_GAME, daily_pool=(1,))
        assert "本局最后一天，结束后金币清零。" in prompts.opening_plan_request({}, game)
        assert "本局最后一天，结束后金币清零。" in prompts.next_match_plan_request(make_logs(1, game), [], game)

    def test_next_match_plan_restores_all_past_match_logs(self):
        logs = [make_log(match_index=1), make_log(match_index=2, is_playoff=True)]
        text = prompts.next_match_plan_request(logs, [], TEST_GAME)
        assert f"第 1 局（正赛局）：\n\n{tables.render_match_log(logs[0])}" in text
        assert f"第 2 局（加赛局）：\n\n{tables.render_match_log(logs[1])}" in text
        assert text.index("出价日志") < text.index("最新累计成绩") < text.index("规划第 3 局")
        assert "之前各局的出价日志会另行提供" in prompts.memory_request({}, 2)

    @pytest.mark.parametrize(
        "build, speech_kind, strategy, bid_task",
        [
            (prompts.opening_plan_request, "开局发言", "规划你第一局的初步策略", "第 1 局第 1 天的出价"),
            (prompts.next_match_plan_request, "最新累计成绩", "规划第 2 局的初步策略", "第 2 局第 1 天的出价"),
        ],
        ids=["opening", "next-match"],
    )
    def test_plans_use_custom_first_day_values(self, build, speech_kind, strategy, bid_task):
        game = replace(TEST_GAME, initial_coins=11, daily_pool=(2, 1, 3), max_bid=9,
                       super_cost=3, super_hit_score=2)
        opening = build is prompts.opening_plan_request
        standings = make_standings({"甲": [2], "乙": [1]}, scores_history={"甲": [10], "乙": [11]})
        text = build({"甲": "发言A", "乙": "发言B"}, game) if opening else build(make_logs(1, game), standings, game)
        assert speech_kind in text
        assert strategy in text and bid_task in text
        if opening:
            assert "【甲】发言A" in text and "【乙】发言B" in text
            assert text.count("全员累计总分：全员 0 分。") == 1
        else:
            assert "局末发言" not in text and "之前整理的记忆" in text
            assert "| 排名 | 玩家 | 总分 | 第1局 | 拍卖分 |" in text
            assert "| 1 | 乙 | 11 | 11 | 1 |" in text
            assert "| 2 | 甲 | 10 | 10 | 2 |" in text
        for fact in ("你有 11 枚金币", "今日奖池为 2 分"):
            assert fact in text

    @pytest.mark.parametrize("opening, match_index", [(True, 1), (False, 2), (False, 3), (False, 4)],
                             ids=["opening", "early-regular", "last-regular", "playoff"])
    def test_plans_only_show_remaining_scores_in_last_regular_match_and_playoffs(self, opening, match_index):
        game = replace(TEST_GAME, daily_pool=(2, 1, 3), super_hit_score=2, regular_matches=3)
        tied = make_standings({"甲": [0], "乙": [0]})
        text = (prompts.opening_plan_request({}, game) if opening
                else prompts.next_match_plan_request(make_logs(match_index - 1, game), tied, game))
        show_remaining = match_index >= game.regular_matches
        assert ("本局还剩" in text) is show_remaining
        assert ("每人最多还能获得" in text) is show_remaining
        if show_remaining:
            assert "\n\n本局还剩 3 天（含今天），剩余拍卖奖池共 6 分。如果每天都命中，每人最多还能获得 6 命中分。" in text
