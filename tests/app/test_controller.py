"""控制器与轮级并发执行器的测试：直接发命令，零网络。共享夹具见 conftest.py。"""

import threading
from contextlib import contextmanager
from copy import deepcopy

import pytest

from auction_game.ai.ai_player import AIPlayer
from pydantic_ai.exceptions import ModelAPIError
from pydantic_ai.messages import ToolCallPart, ToolReturnPart
from tests.support.factories import (
    ALL_NAMES,
    TEST_GAME,
    TEST_PREDICTIONS,
    bid_reply,
    fake_task_runner,
    make_ai_obs,
)
from auction_game.controller import Controller, RoundTask, TournamentAborted, run_parallel_round
from auction_game import controller as controller_module


@contextmanager
def running(controller, fn, *release_events):
    """把线程异常送回主测试，失败时也释放任务和闸门并回收线程。"""
    outcome = {}

    def run():
        try:
            outcome["result"] = fn()
        except BaseException as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=run)
    thread.start()
    try:
        yield thread, outcome
    finally:
        for event in release_events:
            event.set()
        controller.offer_command("q")
        thread.join(timeout=5)
        assert not thread.is_alive(), "控制器线程未退出"
        error = outcome.get("error")
        if error is not None and not isinstance(error, TournamentAborted):
            raise error


def make_named_player(name, task_runner):
    return AIPlayer(name, "测试人设", "测试人设正文", TEST_PREDICTIONS, ALL_NAMES, task_runner, game=TEST_GAME)


class TestController:
    def test_step_mode_gates_each_call(self):
        gate = threading.Event()
        controller = Controller(mode="step", announce=lambda msg: gate.set())
        with running(controller, lambda: controller.wait_for_turn("测试调用")) as (thread, outcome):
            assert gate.wait(timeout=5)
            assert "result" not in outcome
            controller.offer_command("")
            thread.join(timeout=5)
            assert "result" in outcome

    def test_continuous_mode_passes_through(self):
        controller = Controller(mode="continuous", announce=lambda msg: None)
        controller.wait_for_turn("任意调用")  # 不阻塞，直接返回

    def test_stale_enter_does_not_preconfirm_next_round(self):
        # 单步模式下在上一轮执行期间误按的回车不得预确认下一轮
        first_gate = threading.Event()
        second_gate = threading.Event()

        def announce(msg):
            if "第1轮" in msg:
                first_gate.set()
            elif "第2轮" in msg:
                assert not controller._gate.is_set()  # 已确定丢弃执行期间误按的回车
                second_gate.set()

        controller = Controller(mode="step", announce=announce)

        def run_two_rounds():
            controller.wait_for_turn("第1轮")
            controller.offer_command("")  # 模拟执行期间误按的回车
            controller.wait_for_turn("第2轮")

        with running(controller, run_two_rounds) as (thread, outcome):
            assert first_gate.wait(timeout=5)
            controller.offer_command("")
            assert second_gate.wait(timeout=5)
            assert "result" not in outcome
            controller.offer_command("")
            thread.join(timeout=5)
            assert "result" in outcome

    def test_pause_degenerates_to_step_gate(self):
        # 轮间按 p：不再有 RoundAborted，退化为单步闸门等回车
        gate = threading.Event()
        controller = Controller(
            mode="continuous", announce=lambda msg: gate.set() if msg.startswith("[单步]") else None,
        )
        controller.offer_command("p")
        with running(controller, lambda: controller.wait_for_turn("测试轮")) as (thread, outcome):
            assert gate.wait(timeout=5)
            assert "result" not in outcome
            controller.offer_command("")
            thread.join(timeout=5)
            assert "result" in outcome

    def test_quit_raises_tournament_aborted(self):
        controller = Controller(mode="continuous", announce=lambda msg: None)
        controller.offer_command("q")
        with pytest.raises(TournamentAborted):
            controller.wait_for_turn("测试调用")

    def test_mode_switch_anytime(self):
        first_gate = threading.Event()
        second_gate = threading.Event()
        continuous_passed = threading.Event()
        continue_to_step = threading.Event()

        def announce(msg):
            if msg.startswith("[单步]"):
                (second_gate if "单步轮" in msg else first_gate).set()

        controller = Controller(mode="step", announce=announce)

        def run_rounds():
            controller.wait_for_turn("首轮")
            controller.wait_for_turn("连续轮")  # c 后下一轮也直接通过
            continuous_passed.set()
            assert continue_to_step.wait(timeout=5)
            controller.wait_for_turn("单步轮")

        with running(controller, run_rounds, continue_to_step) as (thread, outcome):
            assert first_gate.wait(timeout=5)
            assert "result" not in outcome
            controller.offer_command("c")
            assert continuous_passed.wait(timeout=5)
            controller.offer_command("s")
            continue_to_step.set()
            assert second_gate.wait(timeout=5)
            assert "result" not in outcome
            controller.offer_command("")
            thread.join(timeout=5)
            assert "result" in outcome


class TestParallelRound:
    def test_non_player_participant_rolls_back_while_stateless_result_is_kept(self, monkeypatch):
        started = threading.Event()
        release = threading.Event()
        stateless_collected = threading.Event()
        gate = threading.Event()
        controller = Controller(mode="continuous", announce=lambda msg: gate.set() if msg.startswith("[单步]") else None)

        class Counter:
            def __init__(self):
                self.value = 0
                self.calls = 0
                self.restored = []

            def checkpoint(self):
                # None 也是有效的不透明快照，控制器必须原样交还。
                return None

            def rollback(self, checkpoint):
                self.restored.append(checkpoint)
                self.value = 0

            def run(self):
                self.calls += 1
                self.value += 1
                started.set()
                assert release.wait(timeout=5)
                return self.value

        counter = Counter()
        stateless_calls = []

        def stateless():
            stateless_calls.append(1)
            return "ready"

        original_as_completed = controller_module.as_completed

        def track_collection(futures):
            for future in original_as_completed(futures):
                yield future
                if future.result() == "ready":
                    stateless_collected.set()

        monkeypatch.setattr(controller_module, "as_completed", track_collection)
        tasks = [RoundTask("counter", counter.run, participant=counter), RoundTask("stateless", stateless)]
        with running(controller, lambda: run_parallel_round(tasks, controller, "通用任务"), release) as (thread, outcome):
            assert started.wait(timeout=5) and stateless_collected.wait(timeout=5)
            controller.offer_command("p")
            release.set()
            assert gate.wait(timeout=5)
            assert counter.value == 0 and counter.restored == [None]
            assert "result" not in outcome
            controller.offer_command("")
            thread.join(timeout=5)
            assert outcome.get("result") == {"stateless": "ready", "counter": 1}
            assert counter.calls == 2 and stateless_calls == [1]

    def test_pause_restores_consumed_plan_before_result_collection(self, monkeypatch):
        ready = threading.Event()
        release_collection = threading.Event()
        gate = threading.Event()
        controller = Controller(
            mode="continuous",
            announce=lambda msg: gate.set() if msg.startswith("[单步]") else None,
        )
        task_runner = fake_task_runner([bid_reply(2, 6)])
        player = make_named_player("Alpha", task_runner)
        player.plan_strategy("请规划", "opening_plan")
        original_as_completed = controller_module.as_completed
        first_pass = True

        def hold_first_collection(futures):
            nonlocal first_pass
            for future in original_as_completed(futures):
                if first_pass:
                    first_pass = False
                    assert future.result() == (2, 6)  # 缓存已消费，控制器尚未收集
                    ready.set()
                    assert release_collection.wait(timeout=5)
                yield future

        monkeypatch.setattr(controller_module, "as_completed", hold_first_collection)
        tasks = [RoundTask("Alpha", lambda: player.decide_bid(make_ai_obs(day=1)), participant=player)]
        with running(controller, lambda: run_parallel_round(tasks, controller, "首日"),
                     release_collection) as (thread, outcome):
            assert ready.wait(timeout=5)
            assert player._pending_day1_bid is None
            controller.offer_command("p")
            release_collection.set()
            assert gate.wait(timeout=5)
            assert player._pending_day1_bid == (2, 6)
            controller.offer_command("")
            thread.join(timeout=5)
            assert outcome.get("result") == {"Alpha": (2, 6)}
            assert len(task_runner.calls) == 1

    @pytest.mark.parametrize("failures, reason", [
        ([ModelAPIError("offline", "service unavailable")], "service unavailable"),
        (["没有工具", "还是没有"], "retries"),
    ], ids=["service", "output"])
    def test_failure_reruns_only_failed_player(self, failures, reason):
        gate = threading.Event()
        announcements = []

        def announce(msg):
            announcements.append(msg)
            if msg.startswith("[单步]"):
                gate.set()

        controller = Controller(mode="continuous", announce=announce)
        alpha_chat = fake_task_runner([bid_reply(1)])
        alpha = make_named_player("Alpha", alpha_chat)
        bravo_chat = fake_task_runner([*failures, bid_reply(2)])
        bravo = make_named_player("Bravo", bravo_chat)
        tasks = [
            RoundTask(key="Alpha", participant=alpha, fn=lambda: alpha.decide_bid(make_ai_obs())),
            RoundTask(key="Bravo", participant=bravo, fn=lambda: bravo.decide_bid(make_ai_obs())),
        ]
        with running(controller, lambda: run_parallel_round(tasks, controller, "测试轮")) as (thread, outcome):
            assert gate.wait(timeout=5)  # 等闸门提示，避免失败提示后的回车被清除
            assert "result" not in outcome
            assert len(alpha_chat.calls) == 1
            assert len(bravo_chat.calls) == len(failures)
            assert len(bravo.messages) == 1
            bravo_prefix = deepcopy(bravo.messages)
            alpha_snapshot = deepcopy(alpha.messages)
            assert any(msg.startswith("[失败]") and "Bravo" in msg for msg in announcements)
            assert any(reason in msg for msg in announcements)

            controller.offer_command("")
            thread.join(timeout=5)
            assert outcome.get("result") == {"Alpha": (1, None), "Bravo": (2, None)}
            assert len(bravo_chat.calls) == len(failures) + 1
            assert len(alpha_chat.calls) == 1
            assert alpha.messages == alpha_snapshot
            assert bravo.messages[:len(bravo_prefix)] == bravo_prefix
            parts = [p for m in bravo.messages for p in m.parts]
            calls = [p for p in parts if isinstance(p, ToolCallPart)]
            assert len(calls) == 1 and calls[0].args_as_dict()["bid"] == 2
            assert [p.tool_call_id for p in calls] == [
                p.tool_call_id for p in parts if isinstance(p, ToolReturnPart)]

    def test_step_mode_gates_once_and_runs_concurrently(self):
        announcements = []
        gate = threading.Event()

        def announce(msg):
            announcements.append(msg)
            if msg.startswith("[单步]"):
                gate.set()

        controller = Controller(mode="step", announce=announce)
        barrier = threading.Barrier(6)  # 若被串行执行将超时破碎，以此证明并发
        players = [
            make_named_player(f"P{i}", fake_task_runner([bid_reply(i)]))
            for i in range(6)
        ]

        def make_fn(player):
            def fn():
                barrier.wait(timeout=5)
                return player.decide_bid(make_ai_obs())
            return fn

        tasks = [RoundTask(key=player.name, participant=player, fn=make_fn(player)) for player in players]
        with running(controller, lambda: run_parallel_round(tasks, controller, "测试轮")) as (thread, outcome):
            assert gate.wait(timeout=5)
            assert "result" not in outcome
            controller.offer_command("")
            thread.join(timeout=5)
            assert outcome.get("result") == {f"P{i}": (i, None) for i in range(6)}
            step_gates = [msg for msg in announcements if msg.startswith("[单步]")]
            assert len(step_gates) == 1
            assert "测试轮" in step_gates[0]

    def test_pause_keeps_completed_and_reruns_unfinished(self, monkeypatch):
        gate_announced = threading.Event()

        def announce(msg):
            if msg.startswith("[单步]"):
                gate_announced.set()

        controller = Controller(mode="continuous", announce=announce)
        alpha_collected = threading.Event()
        alpha_chat = fake_task_runner([bid_reply(1)])
        alpha = make_named_player("Alpha", alpha_chat)

        original_as_completed = controller_module.as_completed

        def track_collection(futures):
            for future in original_as_completed(futures):
                yield future
                # yield 返回时控制器已执行 collected[key] = future.result()
                if future.result() == (1, None):
                    alpha_collected.set()

        monkeypatch.setattr(controller_module, "as_completed", track_collection)

        release_bravo = threading.Event()
        bravo_started = threading.Event()
        bravo_calls = []

        def bravo_chat(messages):
            bravo_calls.append(deepcopy(messages))
            bravo_started.set()
            assert release_bravo.wait(timeout=5)
            return bid_reply(2)

        bravo = make_named_player("Bravo", fake_task_runner([bravo_chat, bravo_chat]))
        tasks = [
            RoundTask(key="Alpha", participant=alpha, fn=lambda: alpha.decide_bid(make_ai_obs())),
            RoundTask(key="Bravo", participant=bravo, fn=lambda: bravo.decide_bid(make_ai_obs())),
        ]
        with running(controller, lambda: run_parallel_round(tasks, controller, "测试轮"),
                     release_bravo) as (thread, outcome):
            assert alpha_collected.wait(timeout=5)
            assert bravo_started.wait(timeout=5)
            controller.offer_command("p")
            release_bravo.set()
            assert gate_announced.wait(timeout=5)
            assert "result" not in outcome
            alpha_snapshot = deepcopy(alpha.messages)
            assert len(bravo.messages) == 1
            bravo_prefix = deepcopy(bravo.messages)

            controller.offer_command("")
            thread.join(timeout=5)
            assert outcome.get("result") == {"Alpha": (1, None), "Bravo": (2, None)}
            assert len(alpha_chat.calls) == 1
            assert len(bravo_calls) == 2
            assert [p.content for m in bravo_calls[0] for p in m.parts] == [p.content for m in bravo_calls[1] for p in m.parts]
            assert alpha.messages == alpha_snapshot
            assert bravo.messages[:len(bravo_prefix)] == bravo_prefix
            parts = [p for m in bravo.messages for p in m.parts]
            calls = [p for p in parts if isinstance(p, ToolCallPart)]
            assert len(calls) == 1 and calls[0].args_as_dict()["bid"] == 2
            assert [p.tool_call_id for p in calls] == [
                p.tool_call_id for p in parts if isinstance(p, ToolReturnPart)]
