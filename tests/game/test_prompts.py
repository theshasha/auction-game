"""提示词文案的测试：system 提示词、发言格式、加赛预告、
规划与首日出价合并、非法出价提醒重试。
每日出价消息见 test_prompts_bid_request.py；表格渲染见 test_prompts_render.py；
规则文本与局末重申见 test_prompts_rules.py；共享工厂见 conftest.py。"""

from dataclasses import replace

import pytest

from auction_game.ai import prompts
from tests.support.factories import NAMES, TEST_GAME, make_log, make_standings


class TestSystemPrompt:
    """system 提示词：规则全文 + 身份 + 人设 + 全员公开的赛前预测 + 人设作用范围。"""

    def test_includes_persona_and_scope(self):
        system = prompts.build_system_prompt(
            "甲", "傲娇大小姐", "自称本小姐。", [(name, 10) for name in NAMES], NAMES, TEST_GAME
        )
        assert "你叫 甲" in system
        assert "傲娇大小姐：自称本小姐。" in system
        # 人设只影响说话方式，思考要求保持认真
        assert "不要被人设的说话方式影响你的思考" in system
        assert "发言中的承诺由玩家自行决定是否履行，不会改变游戏规则" in system
        assert "<!--" not in system and "RULES.template.md" not in system

    def test_predictions_public_to_everyone(self):
        predictions = [("甲", 50), ("乙", 20), ("丙", 10), ("丁", 10), ("戊", 10), ("己", 0)]
        for name in NAMES:
            system = prompts.build_system_prompt(name, "人设", "正文", predictions, NAMES, TEST_GAME)
            assert "甲 50%" in system and "己 0%" in system  # 全员可见，含他人与自己的
        system = prompts.build_system_prompt("甲", "人设", "正文", predictions, NAMES, TEST_GAME)
        assert "乙、丙、丁、戊、己" in system  # 其他五位玩家名单

    @pytest.mark.parametrize("enable_predictions", [False, True])
    @pytest.mark.parametrize("enable_personas", [False, True])
    def test_independent_switches_remove_entire_sections(self, enable_predictions, enable_personas):
        from auction_game.game.rules import render_rules

        text = prompts.build_system_prompt(
            "甲", "独特人设标题", "独特人设正文", [("乙", 73)], NAMES, TEST_GAME,
            enable_predictions=enable_predictions, enable_personas=enable_personas,
        )
        for marker in ("【赛前预测】", "专家", "夺冠率预测", "乙 73%"):
            assert (marker in text) is enable_predictions
        for marker in ("【你的人设】", "独特人设标题", "独特人设正文", "人设", "认真思考"):
            assert (marker in text) is enable_personas
        assert "你叫 甲" in text and "乙、丙、丁、戊、己" in text
        assert text.startswith(render_rules(TEST_GAME, NAMES, include_note=False))


class TestSpeechFields:
    """公开字段边界；观众总结提示信息不变。"""

    def test_public_speech_requests_ask_for_speech_block(self):
        auction_scores = {name: 3 if name == "甲" else 0 for name in NAMES}
        scores = auction_scores.copy()
        standings = make_standings({name: [auction_scores[name]] for name in NAMES},
                                   scores_history={name: [scores[name]] for name in NAMES})
        for text in (
            prompts.opening_speech_request(),
            prompts.match_end_speech_request(make_log(auction_scores=auction_scores, scores=scores), standings),
            prompts.final_speech_request(standings, make_log(match_index=5, auction_scores=auction_scores, scores=scores)),
        ):
            assert "strategy" in text and "speech" in text
            assert "strategy 块是发言策略，仅自己可见" in text
            assert "speech 块是公开发言，其他玩家都能看到" in text

    @pytest.mark.parametrize("final", [False, True], ids=["match-end", "final"])
    def test_results_show_settled_scores_and_score_based_ranking(self, final):
        auction_scores = {name: 3 if name == "甲" else 0 for name in NAMES}
        scores = auction_scores.copy()
        log = make_log(auction_scores=auction_scores, scores=scores)
        standings = make_standings({"甲": [2, 1], "乙": [1, 0]},
                                   scores_history={"甲": [8, 4], "乙": [12, 2]})
        text = (prompts.final_speech_request(standings, log) if final
                else prompts.match_end_speech_request(log, standings))
        assert "本局得分：甲 3 分、乙 0 分" in text
        assert "排名与分数明细" in text
        assert f"本局完整出价日志：\n\n{prompts.render_match_log(log)}" in text
        assert f"排名与分数明细：\n\n{prompts.render_standings(standings)}" in text
        assert "| 1 | 乙 | 14 | 12 | 2 | 1 |" in text
        assert "| 2 | 甲 | 12 | 8 | 4 | 3 |" in text
        assert "胜场" not in text

    def test_final_summary_is_private_and_includes_speeches(self):
        text = prompts.final_summary_request({"甲": "感言A", "乙": "感言B"})
        assert "【甲】感言A" in text
        assert "【乙】感言B" in text
        assert "只给观众看的总结发言" in text
        assert "不会给其他玩家看到" in text


@pytest.mark.parametrize("task", ["plan", "bid", "speech", "memory", "summary"])
def test_output_reminder_uses_current_text_protocol(task):
    from auction_game.ai.text_output import output_requirement
    text = prompts.with_output_requirement("任务正文", task)
    assert text == "任务正文\n\n" + output_requirement(task)
    assert "工具" not in text


def test_memory_body_keeps_task_boundaries_without_tool_instruction():
    text = prompts.memory_request({"乙": "公开信号", "丙": "另一条公开信号"})
    assert "save_memory" not in text
    assert "公开发言：\n\n【乙】公开信号\n\n【丙】另一条公开信号\n\n请结合" in text
    for required in ("【乙】公开信号", "完整替换旧记忆", "下一局你看不到之前各局的原始对话和公开发言",
                     "保留之前仍然有用的经验", "修正已经失效的判断", "区分事实与猜测",
                     "篇幅可以根据有用信息的多少调整，无需凑字数或精确计字",
                     "直接返回笔记正文", "规则与最新成绩会另行提供"):
        assert required in text
    assert "支持或推翻判断的关键例子" in text
    assert "不要逐日复述或完整抄录出价序列" in text
    assert "具体报价仅作为判断的必要证据保留" in text


def test_memory_description_keeps_replacement_without_full_record_instruction():
    from auction_game.ai.agent_tasks import MemoryOutput

    text = MemoryOutput.model_fields["memory"].description
    assert "私人笔记" in text and "替换旧记忆" in text
    assert "完整" not in text

class TestPlayoffNotice:
    """局末发言只在还有下一局时发出；正赛打完后还有下一局就预告加赛。"""

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
                auction_scores={name: 3 if name == "甲" else 0 for name in NAMES},
                scores={name: 4 if name == "甲" else 0 for name in NAMES},
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


class TestPlanBidMerge:
    def test_single_day_plan_reminds_coins_reset(self):
        game = replace(TEST_GAME, daily_pool=(1,))
        assert "本局最后一天，结束后金币清零。" in prompts.opening_plan_request({}, game)
        assert "本局最后一天，结束后金币清零。" in prompts.next_match_plan_request([], game, 2)

    """规划请求保留首日资源、权威分数与策略要求；schema 定义格式。"""

    def test_bid_format_hint_only_requests_current_task(self):
        text = prompts.bid_format_hint(TEST_GAME)
        assert text == "请分析局势并提交今日出价。"

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
        text = build({"甲": "发言A", "乙": "发言B"}, game) if opening else build(standings, game, 2)
        assert speech_kind in text
        assert strategy in text and bid_task in text
        if opening:
            assert "【甲】发言A" in text and "【乙】发言B" in text
            assert text.count("\n\n全员累计总分：全员 0 分。\n\n") == 1
        else:
            assert "局末发言" not in text and "之前保存的私人笔记" in text
            assert "| 排名 | 玩家 | 总分 | 第1局 | 拍卖分 |" in text
            assert "| 1 | 乙 | 11 | 11 | 1 |" in text
            assert "| 2 | 甲 | 10 | 10 | 2 |" in text
            assert "全员累计总分：" not in text
        for fact in ("你有 11 枚金币", "今日奖池为 2 分"):
            assert fact in text
        assert '请分析局势并提交今日出价' in text
        assert "```json" not in text

    @pytest.mark.parametrize("opening, match_index", [(True, 1), (False, 2), (False, 3), (False, 4)],
                             ids=["opening", "early-regular", "last-regular", "playoff"])
    def test_plans_only_show_remaining_scores_in_last_regular_match_and_playoffs(self, opening, match_index):
        game = replace(TEST_GAME, daily_pool=(2, 1, 3), super_hit_score=2, regular_matches=3)
        text = (prompts.opening_plan_request({}, game) if opening
                else prompts.next_match_plan_request([], game, match_index))
        show_remaining = match_index >= game.regular_matches
        assert ("本局还剩" in text) is show_remaining
        assert ("每人最多还能获得" in text) is show_remaining
        if show_remaining:
            assert "\n\n本局还剩 3 天（含今天），剩余拍卖奖池共 6 分。如果每天都命中，每人最多还能获得 6 命中分。" in text
