"""公开发言与「规划+首日出价合并」的 AI 玩家测试。共享夹具见 conftest.py。"""

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

import ai_speech
from ai_player import CallFailedError, ParseFailedError
from conftest import (
    bid_reply,
    fake_chat_fn,
    legacy_speech_reply,
    make_ai_obs,
    make_player,
    speech_reply,
)
from game_rules import GameSettings
from players import ScriptedPlayer


class TestSpeechDirector:
    """发言编排向开局和下局规划传入实际参数。"""

    def test_passes_game_to_both_plans(self, monkeypatch, tmp_path):
        game = GameSettings(13, 4, (2, 1, 3), 9, 1, 5, 2, 0)
        players = [ScriptedPlayer.constant(name, 0) for name in ("甲", "乙")]
        director = ai_speech.AISpeechDirector(players, None, tmp_path, game=game)
        plans = []

        def request(others, actual_game):
            assert actual_game is game
            plans.append(others)
            return "规划"

        monkeypatch.setattr(ai_speech.prompts, "opening_plan_request", request)
        monkeypatch.setattr(ai_speech.prompts, "next_match_plan_request", request)
        monkeypatch.setattr(ai_speech.prompts, "match_end_speech_request", lambda *args: "局末发言")
        monkeypatch.setattr(director, "_run_round", lambda *args: {"甲": "发言甲", "乙": "发言乙"})
        monkeypatch.setattr(director, "_publish_speeches", lambda *args: None)

        director.on_opening()
        director.on_match_end(SimpleNamespace(match_index=1), [], True)
        assert plans == [{"乙": "发言乙"}, {"甲": "发言甲"}] * 2
        director.on_match_end(SimpleNamespace(match_index=2), [], False)
        assert len(plans) == 4


class TestSpeech:
    """公开发言：先规划策略、末尾【公开发言】…【发言结束】给出实际公开内容
    （旧裸 JSON 格式保留兼容，见 parse_speech 兜底路径）。

    只公开发言块内容；assistant 全文（含规划）照常进自己的对话线与日志。
    """

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

    def test_legacy_json_speech_still_parsed(self):
        reply = legacy_speech_reply("旧格式发言")
        player = make_player(fake_chat_fn([reply]))
        assert player.give_speech("请发言", "opening_speech") == "旧格式发言"

    def test_speech_returns_speech_field_only(self, tmp_path):
        long_speech = "很" * 600
        reply = speech_reply(long_speech)
        chat_fn = fake_chat_fn([reply])
        player = make_player(chat_fn, tmp_path)
        speech = player.give_speech("请发言", "opening_speech")
        assert speech == long_speech  # 公开面只有发言块内容，不含规划部分
        assert player.messages[-1]["content"] == reply  # 全文（含规划）进自己的对话线
        assert len(chat_fn.calls) == 1
        entries = [json.loads(line) for line in player.log_path.read_text(encoding="utf-8").splitlines()]
        assert entries[0]["content"] == reply
        assert entries[1] == {"phase": "opening_speech", "kind": "speech", "result": long_speech}
        assert not any(entry.get("truncated") for entry in entries)
        md_text = player.log_path.with_suffix(".md").read_text(encoding="utf-8")
        assert reply in md_text
        assert f"> 公开发言内容：{long_speech}" in md_text

    def test_fenced_json_block_speech_parsed(self):
        reply = "发言策略：……\n```json\n{\"speech\": \"围栏里的发言\"}\n```"
        player = make_player(fake_chat_fn([reply]))
        assert player.give_speech("请发言", "opening_speech") == "围栏里的发言"

    def test_multiline_speech_with_literal_newlines_parsed(self):
        # 发言常为未转义的多段文本：speech 解析宽容字面换行（strict=False）
        reply = '策略\n{"speech": "第一段\n第二段"}'
        player = make_player(fake_chat_fn([reply]))
        assert player.give_speech("请发言", "match_1_speech") == "第一段\n第二段"

    @pytest.mark.parametrize("invalid_reply", ["忘了给发言块", "【公开发言】\n【发言结束】"],
                             ids=["missing", "empty-block"])
    def test_parse_failure_retries_once(self, tmp_path, invalid_reply):
        chat_fn = fake_chat_fn([invalid_reply, speech_reply("补上的发言")])
        player = make_player(chat_fn, tmp_path)
        assert player.give_speech("请发言", "opening_speech") == "补上的发言"
        assert len(chat_fn.calls) == 2
        assert "缺少合法的发言块" in player.messages[-2]["content"]

    def test_double_parse_failure_escalates(self, tmp_path):
        # 重试仍无合法发言块：抛 ParseFailedError 升级轮级失败（挂起等人工重跑），
        # 不把无法解析的回复全文公开给其他玩家（实证 gemini-3.7-flash 渠道退化时
        # 垃圾全文经 fallback 污染了全场对话线）；原文已由 _call 完整记入日志
        chat_fn = fake_chat_fn(["没有发言块", "直接就是发言全文"])
        player = make_player(chat_fn, tmp_path)
        with pytest.raises(ParseFailedError) as exc_info:
            player.give_speech("请发言", "opening_speech")
        assert "连续两次解析失败" in str(exc_info.value)
        assert len(chat_fn.calls) == 2
        assert "缺少合法的发言块" in player.messages[-2]["content"]
        log_entries = [json.loads(line) for line in player.log_path.read_text(encoding="utf-8").splitlines()]
        assert any(entry.get("parse_failed") for entry in log_entries)
        assert not any(entry.get("kind") == "speech_fallback" for entry in log_entries)

    def test_final_summary_is_complete_and_not_added_to_context(self, tmp_path):
        long_summary = "长" * 700
        chat_fn = fake_chat_fn([long_summary])
        player = make_player(chat_fn, tmp_path)
        before = deepcopy(player.messages)
        assert player.final_summary("请写观众总结") == long_summary
        assert player.messages == before  # 连 user 消息也不回喂自己的对话线
        assert len(chat_fn.calls) == 1
        assert chat_fn.calls[0] == before + [{"role": "user", "content": "请写观众总结"}]
        entries = [json.loads(line) for line in player.log_path.read_text(encoding="utf-8").splitlines()]
        assert len(entries) == 1
        assert entries[0]["content"] == long_summary
        assert long_summary in player.log_path.with_suffix(".md").read_text(encoding="utf-8")

    def test_speech_double_error_raises(self):
        chat_fn = fake_chat_fn([RuntimeError("timeout"), RuntimeError("timeout")])
        player = make_player(chat_fn)
        before = deepcopy(player.messages)
        with pytest.raises(CallFailedError, match="RuntimeError: timeout"):
            player.give_speech("请发言", "opening_speech")
        assert len(chat_fn.calls) == 2
        assert player.messages == before

    @pytest.mark.parametrize("failure, reason", [
        (RuntimeError("boom"), "RuntimeError: boom"),
        ("", "EmptyResponseError"),
    ], ids=["exception", "empty"])
    def test_final_summary_double_error_raises(self, failure, reason):
        chat_fn = fake_chat_fn([failure, failure])
        player = make_player(chat_fn)
        before = deepcopy(player.messages)
        with pytest.raises(CallFailedError, match=reason):
            player.final_summary("请写观众总结")
        assert len(chat_fn.calls) == 2
        assert player.messages == before
        assert chat_fn.calls[0] == chat_fn.calls[1]

    def test_final_summary_empty_reply_retries(self):
        # 空正文与调用异常同等处理：重试 1 次，第二次正常返回
        chat_fn = fake_chat_fn(["", "给观众的话"])
        player = make_player(chat_fn)
        before = deepcopy(player.messages)
        assert player.final_summary("请写观众总结") == "给观众的话"
        assert len(chat_fn.calls) == 2
        assert player.messages == before
        assert chat_fn.calls[0] == chat_fn.calls[1]


class TestPlanBidMerge:
    """规划与下一局第 1 天出价合并：规划回复末尾的 JSON 缓存为首日出价。"""

    def test_consumed_super_bid_is_restored_by_rollback(self):
        chat_fn = fake_chat_fn(['{"bid": 2, "super_bid": 6}'])
        player = make_player(chat_fn)
        player.plan_strategy("请规划", "opening_plan")
        checkpoint = player.checkpoint()
        history = deepcopy(player.messages)
        for _ in range(2):
            assert player.decide_bid(make_ai_obs(day=1)) == (2, 6)
            assert player._pending_day1_bid is None
            player.rollback(checkpoint)
            assert player._pending_day1_bid == (2, 6)
            assert player.messages == history
        assert player.decide_bid(make_ai_obs(day=1)) == (2, 6)
        assert len(chat_fn.calls) == 1

    def test_rolled_back_plan_leaves_no_new_cache(self):
        player = make_player(fake_chat_fn([bid_reply(3)]))
        checkpoint = player.checkpoint()
        history = deepcopy(player.messages)
        player.plan_strategy("请规划", "opening_plan")
        assert player._pending_day1_bid == (3, None)
        player.rollback(checkpoint)
        assert player._pending_day1_bid is None
        assert player.messages == history

    @pytest.mark.parametrize("fails", [False, True], ids=["success", "parse-failure"])
    def test_rolled_back_replan_restores_previous_cache(self, fails):
        replies = ["没有 JSON", "还是没有"] if fails else [bid_reply(4)]
        chat_fn = fake_chat_fn(['{"bid": 2, "super_bid": 6}', *replies])
        player = make_player(chat_fn)
        player.plan_strategy("旧规划", "opening_plan")
        checkpoint = player.checkpoint()
        history = deepcopy(player.messages)
        if fails:
            with pytest.raises(ParseFailedError):
                player.plan_strategy("新规划", "next_plan")
            assert player._pending_day1_bid is None
        else:
            player.plan_strategy("新规划", "next_plan")
            assert player._pending_day1_bid == (4, None)
        player.rollback(checkpoint)
        assert player.messages == history
        assert player._pending_day1_bid == (2, 6)
        assert player.decide_bid(make_ai_obs(day=1)) == (2, 6)
        assert len(chat_fn.calls) == 1 + len(replies)

    def test_plan_cache_consumed_only_once(self):
        chat_fn = fake_chat_fn(["策略\n```json\n{\"bid\": 3}\n```", bid_reply(2)])
        player = make_player(chat_fn)
        player.plan_strategy("请规划并出价", "opening_plan")

        assert player.decide_bid(make_ai_obs(day=1)) == (3, None)
        assert len(chat_fn.calls) == 1
        assert player.decide_bid(make_ai_obs(day=1)) == (2, None)  # 同一首日再次请求也只能正常调用
        assert len(chat_fn.calls) == 2

    def test_plan_without_cache_falls_back_to_normal_call(self):
        # 未经过规划环节：第 1 天照常发出价请求
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
        chat_fn = fake_chat_fn([bid_reply(2), "没有块", "还是没有"])
        player = make_player(chat_fn, tmp_path)
        player.plan_strategy("旧规划", "opening_plan")
        with pytest.raises(ParseFailedError):
            player.plan_strategy("请重新规划并出价", "next_plan")
        assert player._pending_day1_bid is None  # 缓存保持清空，等重跑成功后写入
        assert len(chat_fn.calls) == 3
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
        assert len(chat_fn.calls) == 2
