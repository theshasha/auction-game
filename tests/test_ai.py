"""AI 玩家与控制器的测试：全部注入 fake chat_fn / 直接发命令，零网络。"""

import json
import threading

import pytest

from ai_player import AIPlayer, CallFailedError, PARSE_FAILED
from controller import Controller, RoundTask, TournamentAborted, run_parallel_round
from models import Observation

ALL_NAMES = ["Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot"]
TEST_PREDICTIONS = [(name, 10) for name in ALL_NAMES]


def make_obs(day=1, coins=6):
    return Observation(
        my_name="Alpha",
        my_coins=coins,
        my_match_wins=0,
        my_total_wins=0,
        day=day,
        today_pool=1,
        today_history=[],
        standings=[],
        past_match_logs=[],
    )


def fake_chat_fn(script):
    """script: 回复文本或异常的有序列表，每次调用弹出下一个。"""
    queue = list(script)
    calls = []

    def chat_fn(messages):
        calls.append(messages)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    chat_fn.calls = calls
    return chat_fn


def make_player(chat_fn, tmp_path=None):
    log_path = (tmp_path / "ai_Alpha.jsonl") if tmp_path else None
    return AIPlayer("Alpha", "测试人设", "测试人设正文", TEST_PREDICTIONS, ALL_NAMES, chat_fn, log_path=log_path)


def bid_reply(bid):
    return f"我的分析：……\n```json\n{{\"bid\": {bid}}}\n```"


def speech_reply(speech):
    """公开发言回复（当前格式）：先规划策略，末尾定界符块给出实际公开发言。"""
    return f"发言策略：先放个烟雾弹。\n【公开发言】\n{speech}\n【发言结束】"


def legacy_speech_reply(speech):
    """旧格式发言回复：末尾裸 JSON {"speech": ...}（解析器保留兼容）。"""
    return f'发言策略：先放个烟雾弹。\n{{"speech": "{speech}"}}'


class TestBidParsing:
    def test_normal_reply_parsed(self):
        player = make_player(fake_chat_fn([bid_reply(3)]))
        assert player.decide_bid(make_obs()) == 3

    def test_uses_last_json_block(self):
        content = "```json\n{\"bid\": 1}\n```\n改主意了\n```json\n{\"bid\": 4}\n```"
        player = make_player(fake_chat_fn([content]))
        assert player.decide_bid(make_obs()) == 4

    def test_bare_json_without_fence_parsed(self):
        # 提示词示例无围栏，模型直接输出裸 JSON 时必须能解析（实证：deepseek-v4-pro 双失败记 0）
        player = make_player(fake_chat_fn(["我的分析：……\n{\"bid\": 2}"]))
        assert player.decide_bid(make_obs()) == 2

    def test_bare_json_uses_last_valid_object(self):
        content = "先举个例子 {\"bid\": 1}，但改主意了\n{\"bid\": 5}"
        player = make_player(fake_chat_fn([content]))
        assert player.decide_bid(make_obs()) == 5

    def test_bare_json_last_object_without_bid_key_fails(self):
        # 最后一个合法 JSON 无 bid 键：不再向前找，按解析失败处理（重试 1 次后成功）
        script = ["正文引用了 {\"note\": \"x\"} 但没出价", bid_reply(3)]
        chat_fn = fake_chat_fn(script)
        player = make_player(chat_fn)
        assert player.decide_bid(make_obs()) == 3
        assert len(chat_fn.calls) == 2

    def test_missing_json_block_retries_once(self):
        script = [
            "我忘了给 JSON",
            bid_reply(2),
        ]
        chat_fn = fake_chat_fn(script)
        player = make_player(chat_fn)
        assert player.decide_bid(make_obs()) == 2
        assert len(chat_fn.calls) == 2
        retry_user_msg = player.messages[-2]["content"]
        assert "缺少合法的 JSON" in retry_user_msg

    def test_double_parse_failure_returns_zero(self):
        script = ["没有块", "还是没有"]
        player = make_player(fake_chat_fn(script))
        assert player.decide_bid(make_obs()) == 0

    def test_invalid_value_not_retried(self):
        # 数值非法（超上限）由引擎判 illegal，AIPlayer 原样返回且不重试
        chat_fn = fake_chat_fn([bid_reply(9)])
        player = make_player(chat_fn)
        assert player.decide_bid(make_obs()) == 9
        assert len(chat_fn.calls) == 1

    def test_error_then_success_recovers(self):
        # 第一次调用失败（如网络抖动）立即重试，成功后正常出价
        chat_fn = fake_chat_fn([RuntimeError("timeout"), bid_reply(4)])
        player = make_player(chat_fn)
        assert player.decide_bid(make_obs()) == 4
        assert len(chat_fn.calls) == 2
        # 失败的 user 消息已回滚，对话线只有 system + 成功的一问一答
        assert [m["role"] for m in player.messages] == ["system", "user", "assistant"]

    def test_double_error_raises_call_failed(self):
        # 连续两次失败（基础设施故障）：抛 CallFailedError 交给轮级别重跑
        chat_fn = fake_chat_fn([RuntimeError("余额不足"), RuntimeError("余额不足")])
        player = make_player(chat_fn)
        with pytest.raises(CallFailedError) as exc_info:
            player.decide_bid(make_obs())
        assert "连续两次调用失败" in str(exc_info.value)
        assert "余额不足" in str(exc_info.value)
        assert len(chat_fn.calls) == 2
        assert len(player.messages) == 1  # 两次失败的 user 消息均已回滚


class TestConversationLine:
    def test_history_is_append_only(self):
        script = [bid_reply(1), bid_reply(2), bid_reply(3)]
        player = make_player(fake_chat_fn(script))
        prefix_snapshot = list(player.messages)
        player.decide_bid(make_obs(day=1))
        mid_snapshot = list(player.messages)
        player.decide_bid(make_obs(day=2))
        # 任意时刻已有消息逐字节不变
        assert player.messages[: len(prefix_snapshot)] == prefix_snapshot
        assert player.messages[: len(mid_snapshot)] == mid_snapshot
        assert player.messages[0]["role"] == "system"

    def test_content_logged_to_jsonl(self, tmp_path):
        player = make_player(fake_chat_fn([bid_reply(3)]), tmp_path)
        player.decide_bid(make_obs())

        log_entries = [json.loads(line) for line in player.log_path.read_text(encoding="utf-8").splitlines()]
        assert any(entry.get("content") == bid_reply(3) for entry in log_entries)

    def test_rollback_truncates_only_tail(self):
        player = make_player(fake_chat_fn([bid_reply(1), bid_reply(2)]))
        player.decide_bid(make_obs(day=1))
        checkpoint = player.checkpoint()
        prefix = list(player.messages)
        player.decide_bid(make_obs(day=2))
        player.rollback(checkpoint)
        assert player.messages == prefix


class TestMarkdownLog:
    """md 双写：每条 jsonl 日志同步渲染追加到同名 .md（jsonl 断言见既有用例，不动）。"""

    @staticmethod
    def read_md(player) -> str:
        return player.log_path.with_suffix(".md").read_text(encoding="utf-8")

    def test_normal_bid_entry_rendered(self, tmp_path):
        player = make_player(fake_chat_fn([bid_reply(3)]), tmp_path)
        player.decide_bid(make_obs())

        md_text = self.read_md(player)
        assert md_text.startswith("# Alpha 对话记录\n")
        assert "## bid" in md_text
        assert "**【user】**" in md_text and "**【assistant】**" in md_text
        assert '```json\n{"bid": 3}\n```' in md_text  # 正文原样收录，不套外层围栏
        assert "> 出价结果：3" in md_text

    def test_error_entry_rendered(self, tmp_path):
        # 第一次调用失败重试成功：md 含失败备注，且最终问答仍完整
        chat_fn = fake_chat_fn([RuntimeError("timeout"), bid_reply(4)])
        player = make_player(chat_fn, tmp_path)
        assert player.decide_bid(make_obs()) == 4

        md_text = self.read_md(player)
        assert "> 调用失败（bid，第1局第1天，第 1 次）：RuntimeError: timeout" in md_text
        assert "## bid" in md_text

    def test_error_entry_without_context_keeps_plain_format(self, tmp_path):
        # 不传 context 的发言失败：保持旧的纯 phase 格式
        chat_fn = fake_chat_fn([RuntimeError("timeout"), speech_reply("发言内容")])
        player = make_player(chat_fn, tmp_path)
        player.give_speech("请发言", "opening_speech")

        assert "> 调用失败（opening_speech，第 1 次）：RuntimeError: timeout" in self.read_md(player)

    def test_parse_failed_entry_rendered(self, tmp_path):
        player = make_player(fake_chat_fn(["没有块", "还是没有"]), tmp_path)
        assert player.decide_bid(make_obs()) == 0

        assert "> 出价解析失败，按规则记 0" in self.read_md(player)

    def test_speech_result_entry_rendered(self, tmp_path):
        player = make_player(fake_chat_fn([speech_reply("公开内容")]), tmp_path)
        player.give_speech("请发言", "opening_speech")

        assert "> 公开发言内容：公开内容" in self.read_md(player)

    def test_speech_fallback_entry_rendered(self, tmp_path):
        player = make_player(fake_chat_fn(["没有发言块", "直接就是发言全文"]), tmp_path)
        player.give_speech("请发言", "opening_speech")

        assert "> 发言块解析失败，重试回复全文已作为公开发言" in self.read_md(player)


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

    def test_double_parse_failure_falls_back_to_fulltext(self, tmp_path):
        # 重试仍无合法发言块：把重试回复全文当作公开发言（不丢内容）
        player = make_player(fake_chat_fn(["没有发言块", "直接就是发言全文"]), tmp_path)
        speech = player.give_speech("请发言", "opening_speech")
        assert speech == "直接就是发言全文"
        log_entries = [json.loads(line) for line in player.log_path.read_text(encoding="utf-8").splitlines()]
        fallback = next(entry for entry in log_entries if entry.get("kind") == "speech_fallback")
        assert fallback["result"] == "直接就是发言全文"

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
        assert player.decide_bid(make_obs(day=2)) == 4

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

        assert player.decide_bid(make_obs(day=1)) == 3
        assert len(chat_fn.calls) == 1  # 第 1 天不再发 LLM 调用

    def test_plan_cache_consumed_only_once(self):
        chat_fn = fake_chat_fn(["策略\n```json\n{\"bid\": 3}\n```", bid_reply(2)])
        player = make_player(chat_fn)
        player.plan_strategy("请规划并出价", "opening_plan")

        assert player.decide_bid(make_obs(day=1)) == 3
        assert player.decide_bid(make_obs(day=2)) == 2  # 缓存已消费，正常调用
        assert len(chat_fn.calls) == 2

    def test_plan_without_cache_falls_back_to_normal_call(self):
        # 未经过规划环节（如暂停重跑丢失缓存）：第 1 天照常发出价请求
        chat_fn = fake_chat_fn([bid_reply(5)])
        player = make_player(chat_fn)
        assert player.decide_bid(make_obs(day=1)) == 5
        assert len(chat_fn.calls) == 1

    def test_plan_parse_failure_retries_once(self):
        chat_fn = fake_chat_fn(["只有策略没有 JSON", "补出价\n```json\n{\"bid\": 4}\n```"])
        player = make_player(chat_fn)
        player.plan_strategy("请规划并出价", "match_1_plan", "第 1 局局末")

        assert player.decide_bid(make_obs(day=1)) == 4
        assert len(chat_fn.calls) == 2
        assert "缺少合法的 JSON" in player.messages[-2]["content"]

    def test_plan_double_parse_failure_caches_zero(self, tmp_path):
        player = make_player(fake_chat_fn(["没有块", "还是没有"]), tmp_path)
        player.plan_strategy("请规划并出价", "opening_plan")

        assert player.decide_bid(make_obs(day=1)) == 0
        log_entries = [
            json.loads(line) for line in player.log_path.read_text(encoding="utf-8").splitlines()
        ]
        assert any(entry.get("parse_failed") and entry.get("result") == 0 for entry in log_entries)

    def test_replan_overwrites_stale_cache(self):
        # 暂停重跑场景：旧缓存先清后写，不会被残留值污染
        chat_fn = fake_chat_fn(["旧规划\n```json\n{\"bid\": 1}\n```", "新规划\n```json\n{\"bid\": 6}\n```"])
        player = make_player(chat_fn)
        player.plan_strategy("请规划并出价", "match_1_plan")
        player.plan_strategy("请重新规划并出价", "match_2_plan")

        assert player.decide_bid(make_obs(day=1)) == 6


class TestController:
    def test_step_mode_gates_each_call(self):
        controller = Controller(mode="step", announce=lambda msg: None)
        done = []

        def call_once():
            controller.wait_for_turn("测试调用")
            done.append(1)

        thread = threading.Thread(target=call_once)
        thread.start()
        thread.join(timeout=0.2)
        assert not done  # 未确认前阻塞
        controller.offer_command("")  # 回车放行
        thread.join(timeout=1)
        assert done == [1]

    def test_continuous_mode_passes_through(self):
        controller = Controller(mode="continuous", announce=lambda msg: None)
        controller.wait_for_turn("任意调用")  # 不阻塞，直接返回

    def test_stale_enter_does_not_preconfirm_next_round(self):
        # 单步模式下在上一轮执行期间误按的回车不得预确认下一轮
        controller = Controller(mode="step", announce=lambda msg: None)
        done = []

        def run_two_rounds():
            controller.wait_for_turn("第1轮")
            controller.offer_command("")  # 模拟执行期间误按的回车
            controller.wait_for_turn("第2轮")
            done.append(1)

        thread = threading.Thread(target=run_two_rounds)
        thread.start()
        thread.join(timeout=0.2)
        assert not done
        controller.offer_command("")  # 放行第1轮
        thread.join(timeout=0.2)
        assert not done  # 误按的回车已在第2轮闸门处被丢弃，仍阻塞
        controller.offer_command("")  # 本轮提示之后的回车才算确认
        thread.join(timeout=1)
        assert done == [1]

    def test_pause_degenerates_to_step_gate(self):
        # 轮间按 p：不再有 RoundAborted，退化为单步闸门等回车
        controller = Controller(mode="continuous", announce=lambda msg: None)
        controller.offer_command("p")
        done = []

        def call_once():
            controller.wait_for_turn("测试轮")
            done.append(1)

        thread = threading.Thread(target=call_once)
        thread.start()
        thread.join(timeout=0.2)
        assert not done  # 暂停被消费为单步闸门：未确认前阻塞
        assert controller._mode == "step"
        controller.offer_command("")
        thread.join(timeout=1)
        assert done == [1]

    def test_quit_raises_tournament_aborted(self):
        controller = Controller(mode="continuous", announce=lambda msg: None)
        controller.offer_command("q")
        with pytest.raises(TournamentAborted):
            controller.wait_for_turn("测试调用")

    def test_mode_switch_anytime(self):
        controller = Controller(mode="step", announce=lambda msg: None)
        controller.offer_command("c")
        assert controller._mode == "continuous"
        controller.offer_command("s")
        assert controller._mode == "step"


class TestParallelRound:
    def test_completed_round_returns_all_results(self):
        controller = Controller(mode="continuous", announce=lambda msg: None)
        tasks = [
            RoundTask(key=str(i), player=None, fn=lambda i=i: i * 10)
            for i in range(6)
        ]
        results = run_parallel_round(tasks, controller, label="测试轮")
        assert results == {str(i): i * 10 for i in range(6)}

    def test_step_mode_gates_once_and_runs_concurrently(self):
        announcements = []
        controller = Controller(mode="step", announce=announcements.append)
        barrier = threading.Barrier(6)  # 若被串行执行将超时破碎，以此证明并发
        players = [
            AIPlayer(f"P{i}", "测试人设", "测试人设正文", TEST_PREDICTIONS, ALL_NAMES, fake_chat_fn([bid_reply(i)]))
            for i in range(6)
        ]

        def make_fn(player):
            def fn():
                barrier.wait(timeout=5)
                return player.decide_bid(make_obs())
            return fn

        tasks = [RoundTask(key=player.name, player=player, fn=make_fn(player)) for player in players]
        outcome = {}

        def run():
            outcome["results"] = run_parallel_round(tasks, controller, label="测试轮")

        thread = threading.Thread(target=run)
        thread.start()
        thread.join(timeout=0.3)
        assert "results" not in outcome  # 闸门未放行
        controller.offer_command("")     # 回车一次放行整轮
        thread.join(timeout=5)
        assert outcome["results"] == {f"P{i}": i for i in range(6)}
        step_gates = [m for m in announcements if m.startswith("[单步]")]
        assert len(step_gates) == 1      # 整轮只闸门一次
        assert "测试轮" in step_gates[0]

    def test_partial_failure_retries_only_failed(self):
        failure_announced = threading.Event()
        announcements = []

        def announce(msg):
            announcements.append(msg)
            if msg.startswith("[失败]"):
                failure_announced.set()

        controller = Controller(mode="continuous", announce=announce)
        alpha_chat = fake_chat_fn([bid_reply(1)])
        alpha = AIPlayer("Alpha", "测试人设", "测试人设正文", TEST_PREDICTIONS, ALL_NAMES, alpha_chat)
        bravo_chat = fake_chat_fn([RuntimeError("超时"), RuntimeError("余额不足"), bid_reply(2)])
        bravo = AIPlayer("Bravo", "测试人设", "测试人设正文", TEST_PREDICTIONS, ALL_NAMES, bravo_chat)
        tasks = [
            RoundTask(key="Alpha", player=alpha, fn=lambda: alpha.decide_bid(make_obs())),
            RoundTask(key="Bravo", player=bravo, fn=lambda: bravo.decide_bid(make_obs())),
        ]
        outcome = {}

        def run():
            outcome["results"] = run_parallel_round(tasks, controller, label="测试轮")

        thread = threading.Thread(target=run)
        thread.start()
        assert failure_announced.wait(timeout=5)  # 连续模式下双失败也会停下来等用户
        thread.join(timeout=0.3)
        assert "results" not in outcome           # 回车前不重跑
        assert len(alpha_chat.calls) == 1
        assert len(bravo_chat.calls) == 2         # 玩家内部已重试 1 次
        alpha_snapshot = list(alpha.messages)

        controller.offer_command("")              # 回车仅重跑失败的 Bravo
        thread.join(timeout=5)
        assert outcome["results"] == {"Alpha": 1, "Bravo": 2}
        assert len(bravo_chat.calls) == 3
        assert len(alpha_chat.calls) == 1         # Alpha 未被重跑
        assert alpha.messages == alpha_snapshot   # 成功者对话线原样保留
        assert [m["role"] for m in bravo.messages] == ["system", "user", "assistant"]
        assert any("余额不足" in m for m in announcements)  # 失败原因已展示

    def test_pause_keeps_completed_and_reruns_unfinished(self):
        gate_announced = threading.Event()

        def announce(msg):
            if msg.startswith("[单步]"):
                gate_announced.set()

        controller = Controller(mode="continuous", announce=announce)
        alpha_done = threading.Event()
        alpha_chat = fake_chat_fn([bid_reply(1)])
        alpha = AIPlayer("Alpha", "测试人设", "测试人设正文", TEST_PREDICTIONS, ALL_NAMES, alpha_chat)

        def alpha_fn():
            result = alpha.decide_bid(make_obs())
            alpha_done.set()
            return result

        release_bravo = threading.Event()
        bravo_calls = []

        def bravo_chat(messages):
            bravo_calls.append(messages)
            release_bravo.wait(timeout=5)
            return bid_reply(2)

        bravo = AIPlayer("Bravo", "测试人设", "测试人设正文", TEST_PREDICTIONS, ALL_NAMES, bravo_chat)
        tasks = [
            RoundTask(key="Alpha", player=alpha, fn=alpha_fn),
            RoundTask(key="Bravo", player=bravo, fn=lambda: bravo.decide_bid(make_obs())),
        ]
        outcome = {}

        def run():
            outcome["results"] = run_parallel_round(tasks, controller, label="测试轮")

        thread = threading.Thread(target=run)
        thread.start()
        assert alpha_done.wait(timeout=5)
        gate_announced.wait(timeout=0.2)  # 给收集循环留出处理 Alpha 结果的时间
        controller.offer_command("p")     # Alpha 已完成、Bravo 在途时暂停
        release_bravo.set()
        assert gate_announced.wait(timeout=5)  # 暂停后切单步，闸门提示补跑
        thread.join(timeout=0.3)
        assert "results" not in outcome
        alpha_snapshot = list(alpha.messages)

        controller.offer_command("")      # 恢复：仅补跑 Bravo
        thread.join(timeout=5)
        assert outcome["results"] == {"Alpha": 1, "Bravo": 2}
        assert len(alpha_chat.calls) == 1  # 已完成者保留，不重跑
        assert len(bravo_calls) == 2       # 在途结果被丢弃，恢复后重跑
        assert alpha.messages == alpha_snapshot
        assert [m["role"] for m in bravo.messages] == ["system", "user", "assistant"]
