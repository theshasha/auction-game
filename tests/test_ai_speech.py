"""公开发言与「规划+首日出价合并」的 AI 玩家测试。共享夹具见 conftest.py。"""

import json

import pytest

from ai_player import CallFailedError, ParseFailedError
from conftest import (
    bid_reply,
    fake_chat_fn,
    legacy_speech_reply,
    make_ai_obs,
    make_player,
    speech_reply,
)


class TestSpeech:
    """公开发言：先规划策略、末尾【公开发言】…【发言结束】给出实际公开内容
    （旧裸 JSON 格式保留兼容，见 parse_speech 兜底路径）。

    只公开发言块内容；assistant 全文（含规划）照常进自己的对话线与日志。
    """

    def test_delimiter_block_parsed(self):
        reply = "发言策略：……\n【公开发言】\n各位好，我来了。\n【发言结束】"
        player = make_player(fake_chat_fn([reply]))
        assert player.give_speech("请发言", "opening_speech") == "各位好，我来了。"

    def test_delimiter_allows_unescaped_quotes(self):
        # 定界符之间是纯文本：内嵌未转义的 " 不再毁掉解析（实证 Kimi 因此解析失败）
        reply = "策略\n【公开发言】\n全场唯一敢说\"6 真香\"的男人。\n【发言结束】"
        player = make_player(fake_chat_fn([reply]))
        assert player.give_speech("请发言", "match_2_speech") == "全场唯一敢说\"6 真香\"的男人。"

    def test_delimiter_allows_multiline(self):
        reply = "策略\n【公开发言】\n第一段。\n\n第二段，随便写。\n【发言结束】"
        player = make_player(fake_chat_fn([reply]))
        assert player.give_speech("请发言", "opening_speech") == "第一段。\n\n第二段，随便写。"

    def test_missing_end_marker_takes_rest_of_reply(self):
        # 缺结束标记：宽容取开始标记到回复末尾（发言按提示词必须放末尾）
        reply = "策略\n【公开发言】\n忘了写结束标记的发言"
        player = make_player(fake_chat_fn([reply]))
        assert player.give_speech("请发言", "opening_speech") == "忘了写结束标记的发言"

    def test_empty_delimiter_block_fails(self):
        # 定界符块为空：视为解析失败，重试后正常
        chat_fn = fake_chat_fn(["【公开发言】\n【发言结束】", speech_reply("补上的发言")])
        player = make_player(chat_fn)
        assert player.give_speech("请发言", "opening_speech") == "补上的发言"
        assert len(chat_fn.calls) == 2

    def test_legacy_json_speech_still_parsed(self):
        reply = legacy_speech_reply("旧格式发言")
        player = make_player(fake_chat_fn([reply]))
        assert player.give_speech("请发言", "opening_speech") == "旧格式发言"

    def test_speech_returns_speech_field_only(self, tmp_path):
        long_speech = "很" * 600
        reply = speech_reply(long_speech)
        player = make_player(fake_chat_fn([reply]), tmp_path)
        speech = player.give_speech("请发言", "opening_speech")
        assert speech == long_speech  # 公开面只有发言块内容，不含规划部分
        assert player.messages[-1]["content"] == reply  # 全文（含规划）进自己的对话线

    def test_fenced_json_block_speech_parsed(self):
        reply = "发言策略：……\n```json\n{\"speech\": \"围栏里的发言\"}\n```"
        player = make_player(fake_chat_fn([reply]))
        assert player.give_speech("请发言", "opening_speech") == "围栏里的发言"

    def test_multiline_speech_with_literal_newlines_parsed(self):
        # 发言常为未转义的多段文本：speech 解析宽容字面换行（strict=False）
        reply = '策略\n{"speech": "第一段\n第二段"}'
        player = make_player(fake_chat_fn([reply]))
        assert player.give_speech("请发言", "match_1_speech") == "第一段\n第二段"

    def test_parse_failure_retries_once(self, tmp_path):
        chat_fn = fake_chat_fn(["忘了给发言块", speech_reply("补上的发言")])
        player = make_player(chat_fn, tmp_path)
        assert player.give_speech("请发言", "opening_speech") == "补上的发言"
        assert len(chat_fn.calls) == 2
        assert "缺少合法的发言块" in player.messages[-2]["content"]

    def test_double_parse_failure_escalates(self, tmp_path):
        # 重试仍无合法发言块：抛 ParseFailedError 升级轮级失败（挂起等人工重跑），
        # 不把无法解析的回复全文公开给其他玩家（实证 gemini-3.7-flash 渠道退化时
        # 垃圾全文经 fallback 污染了全场对话线）；原文已由 _call 完整记入日志
        player = make_player(fake_chat_fn(["没有发言块", "直接就是发言全文"]), tmp_path)
        with pytest.raises(ParseFailedError) as exc_info:
            player.give_speech("请发言", "opening_speech")
        assert "连续两次解析失败" in str(exc_info.value)
        log_entries = [json.loads(line) for line in player.log_path.read_text(encoding="utf-8").splitlines()]
        assert any(entry.get("parse_failed") for entry in log_entries)
        assert not any(entry.get("kind") == "speech_fallback" for entry in log_entries)

    def test_speech_result_logged(self, tmp_path):
        player = make_player(fake_chat_fn([speech_reply("公开内容")]), tmp_path)
        player.give_speech("请发言", "opening_speech")
        log_entries = [json.loads(line) for line in player.log_path.read_text(encoding="utf-8").splitlines()]
        result_entry = next(entry for entry in log_entries if entry.get("kind") == "speech")
        assert result_entry["result"] == "公开内容"

    def test_speech_never_truncated(self, tmp_path):
        # 发言不再截断：全文进对话线，且不产生 truncated 日志
        long_speech = "很" * 600
        player = make_player(fake_chat_fn([speech_reply(long_speech)]), tmp_path)
        speech = player.give_speech("请发言", "opening_speech")
        assert speech == long_speech
        log_entries = [json.loads(line) for line in player.log_path.read_text(encoding="utf-8").splitlines()]
        assert not any(entry.get("truncated") for entry in log_entries)

    def test_bid_error_log_carries_round_context(self, tmp_path):
        chat_fn = fake_chat_fn([RuntimeError("timeout"), bid_reply(4)])
        player = make_player(chat_fn, tmp_path)
        assert player.decide_bid(make_ai_obs(day=2)) == (4, None)

        log_entries = [json.loads(line) for line in player.log_path.read_text(encoding="utf-8").splitlines()]
        error_entry = next(entry for entry in log_entries if "error" in entry)
        assert error_entry["context"] == "第1局第2天"

    def test_final_summary_never_truncated(self):
        long_summary = "长" * 700
        player = make_player(fake_chat_fn([long_summary]))
        assert player.final_summary("请写观众总结") == long_summary

    def test_speech_double_error_raises(self):
        player = make_player(fake_chat_fn([RuntimeError("timeout"), RuntimeError("timeout")]))
        with pytest.raises(CallFailedError):
            player.give_speech("请发言", "opening_speech")

    def test_final_summary_double_error_raises(self):
        player = make_player(fake_chat_fn([RuntimeError("boom"), RuntimeError("boom")]))
        with pytest.raises(CallFailedError):
            player.final_summary("请写观众总结")

    def test_final_summary_empty_reply_retries(self):
        # 空正文与调用异常同等处理：重试 1 次，第二次正常返回
        player = make_player(fake_chat_fn(["", "给观众的话"]))
        assert player.final_summary("请写观众总结") == "给观众的话"

    def test_final_summary_double_empty_raises(self):
        player = make_player(fake_chat_fn(["", ""]))
        with pytest.raises(CallFailedError) as exc_info:
            player.final_summary("请写观众总结")
        assert "EmptyResponseError" in str(exc_info.value)

    def test_final_summary_not_added_to_context(self):
        player = make_player(fake_chat_fn(["给观众的话"]))
        before = list(player.messages)
        summary = player.final_summary("请写观众总结")
        assert summary == "给观众的话"
        assert player.messages == before  # 连 user 消息也不进对话线


class TestPlanBidMerge:
    """规划与下一局第 1 天出价合并：规划回复末尾的 JSON 缓存为首日出价。"""

    def test_plan_caches_day1_bid_consumed_without_call(self):
        chat_fn = fake_chat_fn(["我的策略：……\n```json\n{\"bid\": 3}\n```"])
        player = make_player(chat_fn)
        player.plan_strategy("请规划并出价", "opening_plan")

        assert player.decide_bid(make_ai_obs(day=1)) == (3, None)
        assert len(chat_fn.calls) == 1  # 第 1 天不再发 LLM 调用

    def test_plan_caches_day1_super_bid(self):
        # 规划回复的 JSON 可含 super_bid：第 1 天直接使用超级出价
        chat_fn = fake_chat_fn(['我的策略：首日狙杀\n{"bid": 2, "super_bid": 6}'])
        player = make_player(chat_fn)
        player.plan_strategy("请规划并出价", "opening_plan")

        assert player.decide_bid(make_ai_obs(day=1)) == (2, 6)
        assert len(chat_fn.calls) == 1

    def test_plan_cache_consumed_only_once(self):
        chat_fn = fake_chat_fn(["策略\n```json\n{\"bid\": 3}\n```", bid_reply(2)])
        player = make_player(chat_fn)
        player.plan_strategy("请规划并出价", "opening_plan")

        assert player.decide_bid(make_ai_obs(day=1)) == (3, None)
        assert player.decide_bid(make_ai_obs(day=2)) == (2, None)  # 缓存已消费，正常调用
        assert len(chat_fn.calls) == 2

    def test_plan_without_cache_falls_back_to_normal_call(self):
        # 未经过规划环节（如暂停重跑丢失缓存）：第 1 天照常发出价请求
        chat_fn = fake_chat_fn([bid_reply(5)])
        player = make_player(chat_fn)
        assert player.decide_bid(make_ai_obs(day=1)) == (5, None)
        assert len(chat_fn.calls) == 1

    def test_plan_parse_failure_retries_once(self):
        chat_fn = fake_chat_fn(["只有策略没有 JSON", "补出价\n```json\n{\"bid\": 4}\n```"])
        player = make_player(chat_fn)
        player.plan_strategy("请规划并出价", "match_1_plan", "第 1 局局末")

        assert player.decide_bid(make_ai_obs(day=1)) == (4, None)
        assert len(chat_fn.calls) == 2
        assert "缺少合法的 JSON" in player.messages[-2]["content"]

    def test_plan_double_parse_failure_escalates(self, tmp_path):
        # 规划双重解析失败：抛 ParseFailedError 升级轮级失败，不再缓存 0
        chat_fn = fake_chat_fn(["没有块", "还是没有"])
        player = make_player(chat_fn, tmp_path)
        with pytest.raises(ParseFailedError):
            player.plan_strategy("请规划并出价", "opening_plan")
        assert player._pending_day1_bid is None  # 缓存保持清空，等重跑成功后写入
        log_entries = [
            json.loads(line) for line in player.log_path.read_text(encoding="utf-8").splitlines()
        ]
        assert any(entry.get("parse_failed") and "result" not in entry for entry in log_entries)

    def test_replan_overwrites_stale_cache(self):
        # 暂停重跑场景：旧缓存先清后写，不会被残留值污染
        chat_fn = fake_chat_fn(["旧规划\n```json\n{\"bid\": 1}\n```", "新规划\n```json\n{\"bid\": 6}\n```"])
        player = make_player(chat_fn)
        player.plan_strategy("请规划并出价", "match_1_plan")
        player.plan_strategy("请重新规划并出价", "match_2_plan")

        assert player.decide_bid(make_ai_obs(day=1)) == (6, None)
