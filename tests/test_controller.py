"""控制器与轮级并发执行器的测试：直接发命令，零网络。共享夹具见 conftest.py。"""

import threading

import pytest

from ai_player import AIPlayer
from conftest import (
    ALL_NAMES,
    TEST_PREDICTIONS,
    bid_reply,
    fake_chat_fn,
    make_ai_obs,
)
from controller import Controller, RoundTask, TournamentAborted, run_parallel_round


def make_named_player(name, chat_fn):
    return AIPlayer(name, "测试人设", "测试人设正文", TEST_PREDICTIONS, ALL_NAMES, chat_fn)


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
    def test_parse_failure_escalates_to_round_rerun(self):
        # 解析连续失败（ParseFailedError）与调用失败同路径：轮级挂起等确认，
        # 回车仅重跑失败玩家，成功者不重跑
        failure_announced = threading.Event()
        announcements = []

        def announce(msg):
            announcements.append(msg)
            if msg.startswith("[失败]"):
                failure_announced.set()

        controller = Controller(mode="continuous", announce=announce)
        alpha_chat = fake_chat_fn([bid_reply(1)])
        alpha = make_named_player("Alpha", alpha_chat)
        # Bravo 连续两次输出无 JSON 的垃圾 → ParseFailedError；重跑后正常
        bravo_chat = fake_chat_fn(["没有块", "还是没有", bid_reply(2)])
        bravo = make_named_player("Bravo", bravo_chat)
        tasks = [
            RoundTask(key="Alpha", player=alpha, fn=lambda: alpha.decide_bid(make_ai_obs())),
            RoundTask(key="Bravo", player=bravo, fn=lambda: bravo.decide_bid(make_ai_obs())),
        ]
        outcome = {}

        def run():
            outcome["results"] = run_parallel_round(tasks, controller, label="测试轮")

        thread = threading.Thread(target=run)
        thread.start()
        assert failure_announced.wait(timeout=5)
        thread.join(timeout=0.3)
        assert "results" not in outcome
        assert len(bravo_chat.calls) == 2
        alpha_snapshot = list(alpha.messages)

        controller.offer_command("")              # 回车仅重跑解析失败的 Bravo
        thread.join(timeout=5)
        assert outcome["results"] == {"Alpha": (1, None), "Bravo": (2, None)}
        assert len(bravo_chat.calls) == 3
        assert len(alpha_chat.calls) == 1
        assert alpha.messages == alpha_snapshot
        # 垃圾问答对已随轮级 rollback 清除，重跑后对话线只有成功的一问一答
        assert [m["role"] for m in bravo.messages] == ["system", "user", "assistant"]
        assert any("连续两次解析失败" in m for m in announcements)

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
            make_named_player(f"P{i}", fake_chat_fn([bid_reply(i)]))
            for i in range(6)
        ]

        def make_fn(player):
            def fn():
                barrier.wait(timeout=5)
                return player.decide_bid(make_ai_obs())
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
        assert outcome["results"] == {f"P{i}": (i, None) for i in range(6)}
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
        alpha = make_named_player("Alpha", alpha_chat)
        bravo_chat = fake_chat_fn([RuntimeError("超时"), RuntimeError("余额不足"), bid_reply(2)])
        bravo = make_named_player("Bravo", bravo_chat)
        tasks = [
            RoundTask(key="Alpha", player=alpha, fn=lambda: alpha.decide_bid(make_ai_obs())),
            RoundTask(key="Bravo", player=bravo, fn=lambda: bravo.decide_bid(make_ai_obs())),
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
        assert outcome["results"] == {"Alpha": (1, None), "Bravo": (2, None)}
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
        alpha = make_named_player("Alpha", alpha_chat)

        def alpha_fn():
            result = alpha.decide_bid(make_ai_obs())
            alpha_done.set()
            return result

        release_bravo = threading.Event()
        bravo_calls = []

        def bravo_chat(messages):
            bravo_calls.append(messages)
            release_bravo.wait(timeout=5)
            return bid_reply(2)

        bravo = make_named_player("Bravo", bravo_chat)
        tasks = [
            RoundTask(key="Alpha", player=alpha, fn=alpha_fn),
            RoundTask(key="Bravo", player=bravo, fn=lambda: bravo.decide_bid(make_ai_obs())),
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
        assert outcome["results"] == {"Alpha": (1, None), "Bravo": (2, None)}
        assert len(alpha_chat.calls) == 1  # 已完成者保留，不重跑
        assert len(bravo_calls) == 2       # 在途结果被丢弃，恢复后重跑
        assert alpha.messages == alpha_snapshot
        assert [m["role"] for m in bravo.messages] == ["system", "user", "assistant"]
