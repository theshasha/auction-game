"""提示词文案的测试：system 提示词、发言格式与字数软要求、加赛预告、
发言拼接空行、规划与首日出价合并、非法出价提醒重试。
每日出价消息见 test_prompts_bid_request.py；表格渲染见 test_prompts_render.py；
规则文本与局末重申见 test_prompts_rules.py；共享工厂见 conftest.py。"""

import prompts
from conftest import NAMES, make_log, make_standings


class TestSystemPrompt:
    """system 提示词：规则全文 + 身份 + 人设 + 全员公开的赛前预测 + 人设作用范围。"""

    def test_includes_persona_and_scope(self):
        system = prompts.build_system_prompt(
            "甲", "傲娇大小姐", "自称本小姐。", [(name, 10) for name in NAMES], NAMES
        )
        assert "你叫 甲" in system
        assert "傲娇大小姐：自称本小姐。" in system
        # 人设只影响说话方式，思考要求保持认真
        assert "不要被人设的说话方式影响你的思考" in system

    def test_predictions_public_to_everyone(self):
        predictions = [("甲", 50), ("乙", 20), ("丙", 10), ("丁", 10), ("戊", 10), ("己", 0)]
        for name in NAMES:
            system = prompts.build_system_prompt(name, "人设", "正文", predictions, NAMES)
            assert "甲 50%" in system and "己 0%" in system  # 全员可见，含他人与自己的
        system = prompts.build_system_prompt("甲", "人设", "正文", predictions, NAMES)
        assert "乙、丙、丁、戊、己" in system  # 其他五位玩家名单


class TestSpeechWordLimits:
    """字数只是提示词软要求：开局/局末 300 字，感言/总结不限，规则文本不提字数。"""

    def test_opening_speech_asks_300_chars(self):
        assert "不超过 300 字" in prompts.opening_speech_request()

    def test_match_end_speech_asks_300_chars(self):
        text = prompts.match_end_speech_request(make_log(), make_standings({n: [0] for n in NAMES}), True)
        assert "不超过 300 字" in text

    def test_final_speech_and_summary_have_no_word_limit(self):
        standings = make_standings({n: [0] for n in NAMES})
        assert "字" not in prompts.final_speech_request(standings, make_log(match_index=5))
        assert "字" not in prompts.final_summary_request({"乙": "感言"})

    def test_rules_text_has_no_word_limit(self):
        assert "500 字" not in prompts.RULES_TEXT
        assert "300 字" not in prompts.RULES_TEXT


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

    def test_final_summary_stays_plain_text(self):
        text = prompts.final_summary_request({"乙": "感言"})
        assert "【公开发言】" not in text
        assert "不会给其他玩家看到" in text

    def test_retry_speech_request(self):
        text = prompts.retry_speech_request()
        assert "缺少合法的发言块" in text
        assert "【公开发言】" in text and "【发言结束】" in text


class TestPlayoffNotice:
    """加赛预告只在正赛打完（或加赛后）且第一名仍并列时出现。"""

    def test_no_notice_for_early_matches(self):
        text = prompts.match_end_speech_request(
            make_log(match_index=2), make_standings({n: [0, 0] for n in NAMES}), will_continue=True
        )
        assert "第一名出现并列" not in text
        assert "加赛局" not in text

    def test_notice_on_match5_when_tied(self):
        text = prompts.match_end_speech_request(
            make_log(match_index=5),
            make_standings({n: [0] * 5 for n in NAMES}),
            will_continue=True,
        )
        assert "第一名出现并列" in text
        assert "加赛局" in text

    def test_no_notice_on_match5_when_decided(self):
        text = prompts.match_end_speech_request(
            make_log(match_index=5),
            make_standings({n: [0] * 5 for n in NAMES}),
            will_continue=False,
        )
        assert "第一名出现并列" not in text

    def test_notice_after_playoff_when_still_tied(self):
        text = prompts.match_end_speech_request(
            make_log(match_index=6, is_playoff=True),
            make_standings({n: [0] * 6 for n in NAMES}),
            will_continue=True,
        )
        assert "第一名出现并列" in text


class TestSpeechJoinBlankLines:
    """他人发言拼接：两人之间空一行。"""

    def test_opening_plan_separates_speeches_with_blank_line(self):
        text = prompts.opening_plan_request({"甲": "发言A", "乙": "发言B"})
        assert "【甲】发言A\n\n【乙】发言B" in text

    def test_next_match_plan_separates_speeches_with_blank_line(self):
        text = prompts.next_match_plan_request({"甲": "发言A", "乙": "发言B"})
        assert "【甲】发言A\n\n【乙】发言B" in text

    def test_final_summary_separates_speeches_with_blank_line(self):
        text = prompts.final_summary_request({"甲": "感言A", "乙": "感言B"})
        assert "【甲】感言A\n\n【乙】感言B" in text


class TestPlanBidMerge:
    """规划请求与下一局第 1 天出价合并：附恒定首日状态与 JSON 出价格式（含超级出价）。"""

    def test_opening_plan_asks_for_day1_bid(self):
        text = prompts.opening_plan_request({"乙": "发言"})
        assert "第 1 局第 1 天的出价" in text
        assert "你的金币 6，本局已获胜场 0，今日奖池 1 个胜场，出价上限 6" in text
        assert '{"bid":' in text
        assert "```json" not in text  # 示例无围栏，避免模型误以为必须包代码块

    def test_next_match_plan_asks_for_day1_bid(self):
        text = prompts.next_match_plan_request({"乙": "发言"})
        assert "下一局第 1 天的出价" in text
        assert "你的金币 6，本局已获胜场 0，今日奖池 1 个胜场，出价上限 6" in text
        assert '{"bid":' in text
        assert "```json" not in text

    def test_bid_format_hint_covers_super_bid(self):
        text = prompts.bid_format_hint()
        assert '"super_bid"' in text
        # 规则细节（每局 1 次、不受余额限制、不能获胜等）不在每日消息复述，
        # 由 system 的 RULES.md 与局末重申兜底
        assert "不受余额限制" not in text
        assert "不使用超级出价就不要包含 super_bid 字段" in text
        assert "```json" not in text


class TestInvalidBidRetry:
    """数值非法（解析成功但违反出价规则）的提醒重试：写清具体违规原因。"""

    def test_includes_reason_and_format_hint(self):
        text = prompts.invalid_bid_retry_request("你的出价 6 超过当前余额 4")
        assert "你的出价 6 超过当前余额 4" in text
        assert "请重新出价" in text
        assert '{"bid":' in text  # 附出价格式提示
