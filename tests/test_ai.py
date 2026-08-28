"""AI 玩家与控制器的测试：全部注入 fake chat_fn / 直接发命令，零网络。"""

import json
import threading

import pytest

from ai_player import AIPlayer, CallFailedError, PARSE_FAILED
from controller import Controller, RoundTask, TournamentAborted, run_parallel_round
from models import Observation

ALL_NAMES = ["Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot"]


def make_obs(day=1, coins=6):
    return Observation(
        my_name="Alpha",
        my_coins=coins,
        my_match_wins=0,
        my_total_wins=0,
        day=day,
        wins_remaining=3,
        max_days=5,
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
    return AIPlayer("Alpha", "测试人设", ALL_NAMES, chat_fn, log_path=log_path)


def bid_reply(bid):
    return f"我的分析：……\n```json\n{{\"bid\": {bid}}}\n```"


class TestBidParsing:
    def test_normal_reply_parsed(self):
        player = make_player(fake_chat_fn([bid_reply(3)]))
        assert player.decide_bid(make_obs()) == 3

    def test_uses_last_json_block(self):
        content = "```json\n{\"bid\": 1}\n```\n改主意了\n```json\n{\"bid\": 4}\n```"
        player = make_player(fake_chat_fn([content]))
        assert player.decide_bid(make_obs()) == 4

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
        assert "> 调用失败（bid，第 1 次）：RuntimeError: timeout" in md_text
        assert "## bid" in md_text

    def test_parse_failed_entry_rendered(self, tmp_path):
        player = make_player(fake_chat_fn(["没有块", "还是没有"]), tmp_path)
        assert player.decide_bid(make_obs()) == 0

        assert "> 出价解析失败，按规则记 0" in self.read_md(player)

    def test_truncation_entry_rendered(self, tmp_path):
        player = make_player(fake_chat_fn(["很" * 600]), tmp_path)
        player.give_speech("请发言", "opening_speech")

        md_text = self.read_md(player)
        assert "> 发言截断：原文 600 字，已截断至 500 字" in md_text


class TestSpeech:
    def test_speech_truncated_at_500_chars(self, tmp_path):
        long_speech = "很" * 600
        player = make_player(fake_chat_fn([long_speech]), tmp_path)
        speech = player.give_speech("请发言", "opening_speech")
        assert speech is not None and len(speech) == 500
        log_entries = [json.loads(line) for line in player.log_path.read_text(encoding="utf-8").splitlines()]
        assert any(entry.get("truncated") for entry in log_entries)

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
            AIPlayer(f"P{i}", "测试人设", ALL_NAMES, fake_chat_fn([bid_reply(i)]))
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
        alpha = AIPlayer("Alpha", "测试人设", ALL_NAMES, alpha_chat)
        bravo_chat = fake_chat_fn([RuntimeError("超时"), RuntimeError("余额不足"), bid_reply(2)])
        bravo = AIPlayer("Bravo", "测试人设", ALL_NAMES, bravo_chat)
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
        alpha = AIPlayer("Alpha", "测试人设", ALL_NAMES, alpha_chat)

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

        bravo = AIPlayer("Bravo", "测试人设", ALL_NAMES, bravo_chat)
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
