"""提示词文案的测试：system 提示词、发言格式、加赛预告、
规划与首日出价合并、非法出价提醒重试。
每日出价消息见 test_prompts_bid_request.py；表格渲染见 test_prompts_render.py；
规则文本与局末重申见 test_prompts_rules.py；共享工厂见 conftest.py。"""

from dataclasses import replace

import pytest

import prompts
from conftest import NAMES, TEST_GAME, make_log, make_standings


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

    def test_final_summary_is_private_and_includes_speeches(self):
        text = prompts.final_summary_request({"甲": "感言A", "乙": "感言B"})
        assert "【甲】感言A" in text
        assert "【乙】感言B" in text
        assert "只给观众看的总结发言" in text
        assert "不会给其他玩家看到" in text

    def test_retry_speech_request(self):
        text = prompts.retry_speech_request()
        assert "缺少合法的发言块" in text
        assert "【公开发言】" in text and "【发言结束】" in text


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
    """规划请求与下一局第 1 天出价合并：附恒定首日状态与 JSON 出价格式（含超级出价）。"""

    def test_bid_format_hint_covers_super_bid(self):
        text = prompts.bid_format_hint(TEST_GAME)
        assert '"super_bid"' in text
        # 提交合法性约束（同值、合计余额——v6 违反即整单作废）放模板防无谓非法提交；
        # 策略规则细节（命中判定、悬赏、不能获胜等）不在每日消息复述，
        # 由 system 的 RULES.md 与局末重申兜底
        assert "不得与正常出价同值" in text
        assert "2 金币成本不得超过你的余额" in text
        assert "不使用超级出价就不要包含 super_bid 字段" in text
        assert "```json" not in text

    @pytest.mark.parametrize(
        "build, speech_kind, strategy, bid_task",
        [
            (prompts.opening_plan_request, "开局发言", "规划你第一局的初步策略", "第 1 局第 1 天的出价"),
            (prompts.next_match_plan_request, "局末发言", "规划你下一局的初步策略", "下一局第 1 天的出价"),
        ],
        ids=["opening", "next-match"],
    )
    def test_plans_use_custom_first_day_values(self, build, speech_kind, strategy, bid_task):
        game = replace(TEST_GAME, initial_coins=11, daily_pool=(2, 1, 3), max_bid=9, super_cost=3)
        text = build({"甲": "发言A", "乙": "发言B"}, game)
        assert speech_kind in text
        assert strategy in text and bid_task in text
        assert "【甲】发言A" in text and "【乙】发言B" in text
        assert "你的金币 11，本局已获胜场 0，今日奖池 2 个胜场，出价上限 9" in text
        assert '{"bid": <0到9的整数，不得超过你的余额>}' in text
        assert '{"bid": <正常出价>, "super_bid": <0到9的整数>}' in text
        assert "加 3 金币成本" in text
        assert "```json" not in text


class TestInvalidBidRetry:
    """数值非法（解析成功但违反出价规则）的提醒重试：写清具体违规原因。"""

    def test_includes_reason_and_format_hint(self):
        text = prompts.invalid_bid_retry_request("你的出价 6 超过当前余额 4", TEST_GAME)
        assert "你的出价 6 超过当前余额 4" in text
        assert "请重新出价" in text
        assert '{"bid":' in text  # 附出价格式提示
