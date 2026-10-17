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

    def test_predictions_public_to_everyone(self):
        predictions = [("甲", 50), ("乙", 20), ("丙", 10), ("丁", 10), ("戊", 10), ("己", 0)]
        for name in NAMES:
            system = prompts.build_system_prompt(name, "人设", "正文", predictions, NAMES, TEST_GAME)
            assert "甲 50%" in system and "己 0%" in system  # 全员可见，含他人与自己的
        system = prompts.build_system_prompt("甲", "人设", "正文", predictions, NAMES, TEST_GAME)
        assert "乙、丙、丁、戊、己" in system  # 其他五位玩家名单


class TestSpeechFields:
    """公开字段边界及发言免责句；观众总结提示信息不变。"""

    def test_public_speech_requests_ask_for_speech_block(self):
        standings = make_standings({n: [0] for n in NAMES})
        for text in (
            prompts.opening_speech_request(),
            prompts.match_end_speech_request(make_log(), standings, True),
            prompts.final_speech_request(standings, make_log(match_index=5)),
        ):
            assert "strategy" in text and "speech" in text
            assert "只有 speech 会给其他玩家看" in text
            assert "不构成对你后续出价的任何约束" in text

    def test_final_summary_is_private_and_includes_speeches(self):
        text = prompts.final_summary_request({"甲": "感言A", "乙": "感言B"})
        assert "【甲】感言A" in text
        assert "【乙】感言B" in text
        assert "只给观众看的总结发言" in text
        assert "不会给其他玩家看到" in text

class TestPlayoffNotice:
    """加赛预告只在正赛打完（或加赛后）且第一名仍并列时出现。"""

    @pytest.mark.parametrize(
        "regular_matches, match_index, is_playoff, will_continue, expected",
        [
            (5, 2, False, True, False),
            (5, 5, False, True, True),
            (5, 5, False, False, False),
            (5, 6, True, True, True),
            (2, 2, False, True, True),
            (2, 2, False, False, False),
        ],
        ids=["early", "regular-tied", "regular-decided", "playoff-tied",
             "custom-tied", "custom-decided"],
    )
    def test_notice_at_regular_and_playoff_boundaries(
        self, regular_matches, match_index, is_playoff, will_continue, expected,
    ):
        text = prompts.match_end_speech_request(
            make_log(
                match_index=match_index, is_playoff=is_playoff,
                game=replace(TEST_GAME, regular_matches=regular_matches),
            ),
            make_standings({n: [0] * match_index for n in NAMES}),
            will_continue=will_continue,
        )
        notice = "第一名出现并列，按规则全员将进入加赛局，加赛局胜场计入总胜场。"
        assert ("第一名出现并列" in text) is expected
        if expected:
            assert notice in text


class TestPlanBidMerge:
    def test_single_day_plan_reminds_coins_reset(self):
        game = replace(TEST_GAME, daily_pool=(1,))
        assert "本局最后一天，结束后金币清零。" in prompts.opening_plan_request({}, game)
        assert "本局最后一天，结束后金币清零。" in prompts.next_match_plan_request([], game, 2)

    """规划请求保留恒定首日状态与策略要求；schema 定义格式。"""

    def test_bid_format_hint_covers_super_bid(self):
        text = prompts.bid_format_hint(TEST_GAME)
        assert 'super_bid' in text
        # 提交合法性约束（同值、合计余额——违反即整单作废）放模板防无谓非法提交；
        # 策略规则细节（命中判定、悬赏、不能获胜等）不在每日消息复述，
        # 由 system 的实时渲染规则兜底
        assert "使用超级出价时，两笔出价不得同值" in text
        assert "2 金币成本不得超过余额" in text
        assert "super_bid 明确为 null" in text
        assert "```json" not in text

    @pytest.mark.parametrize(
        "build, speech_kind, strategy, bid_task",
        [
            (prompts.opening_plan_request, "开局发言", "规划你第一局的初步策略", "第 1 局第 1 天的出价"),
            (prompts.next_match_plan_request, "最新累计成绩", "规划第 2 局的初步策略", "第 2 局第 1 天的出价"),
        ],
        ids=["opening", "next-match"],
    )
    def test_plans_use_custom_first_day_values(self, build, speech_kind, strategy, bid_task):
        game = replace(TEST_GAME, initial_coins=11, daily_pool=(2, 1, 3), max_bid=9, super_cost=3)
        opening = build is prompts.opening_plan_request
        text = build({"甲": "发言A", "乙": "发言B"}, game) if opening else build([], game, 2)
        assert speech_kind in text
        assert strategy in text and bid_task in text
        if opening:
            assert "【甲】发言A" in text and "【乙】发言B" in text
        else:
            assert "局末发言" not in text and "累计私有记忆" in text
        assert "你的金币 11，本局已获胜场 0，今日奖池 2 个胜场，规则出价上限 9" in text
        assert '0 到 9 的整数' in text
        assert 'strategy' in text
        assert "加 3 金币成本" in text
        assert "```json" not in text
