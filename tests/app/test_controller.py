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
from auction_game.ai.agent_execution import CallFailedError


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


@pytest.mark.parametrize("command", ["", "q"], ids=["resume-only-failed-memory", "abort-restores-context"])
def test_memory_round_keeps_successful_reset_and_can_restore_whole_round(command):
    gate = threading.Event()

    def announce(message):
        if message.startswith("[单步]"):
            gate.set()

    controller = Controller(mode="continuous", announce=announce)
    alpha = make_named_player("Alpha", fake_task_runner([bid_reply(1), {"memory": "alpha cumulative"}]))
    bravo = make_named_player("Bravo", fake_task_runner([bid_reply(2), ModelAPIError("offline", "unavailable"),
                                                        {"memory": "bravo cumulative"}]))
    players = [alpha, bravo]
    for player in players:
        player.plan_strategy("prior match context", "opening_plan")
    before = [p.checkpoint() for p in players]
    tasks = [RoundTask(key=p.name, participant=p,
                       fn=lambda p=p: p.save_memory("all published speeches", "match_1_memory")) for p in players]
    with running(controller, lambda: run_parallel_round(tasks, controller, "累计记忆")) as (thread, outcome):
        assert gate.wait(timeout=5)
        assert alpha.memory == "alpha cumulative" and len(alpha.messages) == 2
        assert bravo.checkpoint() == before[1]
        successful = alpha.checkpoint()
        controller.offer_command(command)
        thread.join(timeout=5)
        if command == "q":
            assert isinstance(outcome.get("error"), TournamentAborted)
            assert [p.checkpoint() for p in players] == before
        else:
            assert outcome.get("result") == {"Alpha": None, "Bravo": None}
            assert alpha.checkpoint() == successful and bravo.memory == "bravo cumulative"
            assert len(alpha.task_runner.calls) == 2 and len(bravo.task_runner.calls) == 3
            assert alpha._pending_day1_bid is None and bravo._pending_day1_bid is None


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
        # 轮间按 p：等待用户确认继续，不启动下一轮。
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
    @pytest.mark.parametrize("limit", [1, 2, 10])
    def test_bounded_dispatch_and_serial_order(self, limit):
        count = 5
        releases = [threading.Event() for _ in range(count)]
        started = [threading.Event() for _ in range(count)]
        lock = threading.Lock()
        live = 0
        peak = 0
        calls = []
        controller = Controller(mode="continuous", max_concurrency=limit, announce=lambda _: None)

        def task(index):
            nonlocal live, peak
            with lock:
                live += 1
                peak = max(peak, live)
                calls.append(index)
            started[index].set()
            assert releases[index].wait(timeout=5)
            with lock:
                live -= 1
            return index

        tasks = [RoundTask(str(i), lambda i=i: task(i)) for i in range(count)]
        with running(controller, lambda: run_parallel_round(tasks, controller, "有界"), *releases) as (thread, outcome):
            first_batch = min(limit, count)
            assert all(event.wait(timeout=5) for event in started[:first_batch])
            assert not any(event.is_set() for event in started[first_batch:])
            for index in range(count):
                assert started[index].wait(timeout=5)
                releases[index].set()
            thread.join(timeout=5)
            assert outcome.get("result") == {str(i): i for i in range(count)}
            assert peak == first_batch
            if limit == 1:
                assert calls == list(range(count))

    @pytest.mark.parametrize("command", ["", "c"])
    def test_fast_resume_and_repeated_pause_preserve_event(self, command):
        release = threading.Event()
        started = threading.Event()
        announcements = []
        controller = Controller(mode="continuous", max_concurrency=1, announce=announcements.append)
        calls = []

        def first():
            calls.append("first")
            started.set()
            assert release.wait(timeout=5)
            return 1

        def second():
            calls.append("second")
            return 2

        tasks = [RoundTask("first", first), RoundTask("second", second)]
        with running(controller, lambda: run_parallel_round(tasks, controller, "快速恢复"), release) as (thread, outcome):
            assert started.wait(timeout=5)
            controller.offer_command("p")
            controller.offer_command(command)
            controller.offer_command("p")  # 已暂停时重复 p，不清除恢复意图
            release.set()
            thread.join(timeout=5)
            assert outcome.get("result") == {"first": 1, "second": 2}
            assert calls == ["first", "second"]
            assert sum("正在暂停" in message for message in announcements) == 1
            assert any("成功 1，失败 0，未启动 1" in message for message in announcements)
            assert any("不再启动新任务" in message and "任务内部重试可能继续" in message
                       and "撤销本轮并中止" in message for message in announcements)

    def test_pause_keeps_success_and_holds_unstarted_until_resume(self):
        release = threading.Event()
        started = threading.Event()
        gate = threading.Event()
        controller = Controller(mode="continuous", max_concurrency=1,
                                announce=lambda msg: gate.set() if msg.startswith("[单步]") else None)
        calls = []

        def first():
            calls.append("first")
            started.set()
            assert release.wait(timeout=5)
            return 1

        def second():
            calls.append("second")
            return 2

        tasks = [RoundTask("first", first), RoundTask("second", second)]
        with running(controller, lambda: run_parallel_round(tasks, controller, "暂停派发"), release) as (thread, outcome):
            assert started.wait(timeout=5)
            controller.offer_command("p")
            release.set()
            assert gate.wait(timeout=5)
            assert calls == ["first"]
            assert "result" not in outcome
            controller.offer_command("")
            thread.join(timeout=5)
            assert outcome.get("result") == {"first": 1, "second": 2}
            assert calls == ["first", "second"]

    def test_pause_retries_failed_and_unstarted_only_after_new_confirmation(self):
        release = threading.Event()
        first_started = threading.Event()
        pause_requested = threading.Event()
        gate = threading.Event()
        announcements = []

        def announce(message):
            announcements.append(message)
            if message.startswith("[单步]"):
                gate.set()

        controller = Controller(mode="continuous", max_concurrency=2, announce=announce)
        calls = [0, 0, 0]

        def task(index):
            calls[index] += 1
            if index == 0:
                first_started.set()
                assert release.wait(timeout=5)
            if index == 1 and calls[index] == 1:
                controller.offer_command("p")
                controller.offer_command("c")  # 失败发生前的恢复不足以确认重试
                pause_requested.set()
                raise CallFailedError("测试失败")
            return index

        tasks = [RoundTask(str(i), lambda i=i: task(i)) for i in range(3)]
        with running(controller, lambda: run_parallel_round(tasks, controller, "混合"), release) as (thread, outcome):
            assert first_started.wait(timeout=5)
            assert pause_requested.wait(timeout=5)
            release.set()
            assert gate.wait(timeout=5)
            assert "result" not in outcome
            assert calls == [1, 1, 0]
            assert any("成功 1，失败 1，未启动 1" in message for message in announcements)
            assert any("按回车或 c 重试失败任务并执行未启动任务" in message
                       and "成功任务不会重跑" in message for message in announcements)
            controller.offer_command("")
            thread.join(timeout=5)
            assert outcome.get("result") == {"0": 0, "1": 1, "2": 2}
            assert calls == [1, 2, 1]

    @pytest.mark.parametrize("failures", [1, 2])
    @pytest.mark.parametrize("paused", [False, True])
    @pytest.mark.parametrize("command", ["", "c"])
    def test_failure_notice_accepts_immediate_confirmation(self, failures, paused, command):
        notices = [threading.Event() for _ in range(failures)]
        calls = {"success": 0, "retry": 0}
        notice_count = 0

        def announce(message):
            nonlocal notice_count
            if message.startswith("[失败]"):
                assert calls == {"success": 1, "retry": notice_count + 1}
                # 确认发生在公告回调内，早于后续单步提示，不靠线程调度猜时序。
                controller.offer_command(command)
                notices[notice_count].set()
                notice_count += 1

        controller = Controller(mode="continuous", max_concurrency=1, announce=announce)

        def success():
            calls["success"] += 1
            return "kept"

        def retry():
            calls["retry"] += 1
            if calls["retry"] <= failures:
                if paused:
                    controller.offer_command("p")
                    controller.offer_command("c")  # 失败公告前的恢复仍须重新确认。
                raise CallFailedError("测试失败")
            return "recovered"

        tasks = [RoundTask("success", success), RoundTask("retry", retry)]
        with running(controller, lambda: run_parallel_round(tasks, controller, "立即确认")) as (thread, outcome):
            assert all(notice.wait(timeout=5) for notice in notices)
            thread.join(timeout=5)
            assert outcome.get("result") == {"success": "kept", "retry": "recovered"}
            assert calls == {"success": 1, "retry": failures + 1}

    def test_quit_at_failure_notice_does_not_retry(self):
        notice = threading.Event()
        calls = []

        def announce(message):
            if message.startswith("[失败]"):
                controller.offer_command("q")
                notice.set()

        controller = Controller(mode="continuous", announce=announce)

        def fail():
            calls.append(1)
            raise CallFailedError("测试失败")

        with running(controller, lambda: run_parallel_round([RoundTask("fail", fail)], controller, "中止补跑")) as (thread, outcome):
            assert notice.wait(timeout=5)
            thread.join(timeout=5)
            assert isinstance(outcome.get("error"), TournamentAborted)
            assert calls == [1]

    def test_quit_drains_inflight_rolls_back_and_does_not_dispatch_more(self):
        release = threading.Event()
        started = threading.Event()
        controller = Controller(mode="continuous", max_concurrency=1, announce=lambda _: None)

        class Participant:
            value = 0
            restored = []

            def checkpoint(self):
                return None

            def rollback(self, checkpoint):
                self.restored.append(checkpoint)
                self.value = 0

            def run(self):
                started.set()
                assert release.wait(timeout=5)
                self.value = 1
                return 1

        participant = Participant()
        tasks = [RoundTask("first", participant.run, participant),
                 RoundTask("unstarted", lambda: pytest.fail("q 后不得派发"))]
        with running(controller, lambda: run_parallel_round(tasks, controller, "中止"), release) as (thread, outcome):
            assert started.wait(timeout=5)
            controller.offer_command("q")
            assert thread.is_alive()
            release.set()
            thread.join(timeout=5)
            assert isinstance(outcome.get("error"), TournamentAborted)
            assert participant.value == 0
            assert participant.restored == [None]

    def test_program_error_propagates_even_after_pause(self):
        controller = Controller(mode="continuous", announce=lambda _: None)

        def broken():
            controller.offer_command("p")
            raise ValueError("真实程序错误")

        with pytest.raises(ValueError, match="真实程序错误"):
            run_parallel_round([RoundTask("broken", broken)], controller, "程序错误")

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
                if self.calls == 1:
                    raise CallFailedError("测试服务故障")
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
                if future.exception() is None and future.result() == "ready":
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

    def test_pause_keeps_consumed_plan_before_result_collection(self, monkeypatch):
        ready = threading.Event()
        release_collection = threading.Event()
        gate = threading.Event()
        announcements = []

        def announce(message):
            announcements.append(message)
            if message.startswith("[单步]"):
                gate.set()

        controller = Controller(
            mode="continuous",
            announce=announce,
        )
        task_runner = fake_task_runner([bid_reply(2, 6)])
        player = make_named_player("Alpha", task_runner)
        player.plan_strategy("请规划", "opening_plan")
        history = deepcopy(player.messages)
        bid_calls = []

        def consume_plan():
            bid_calls.append(1)
            return player.decide_bid(make_ai_obs(day=1))
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
        tasks = [RoundTask("Alpha", consume_plan, participant=player)]
        with running(controller, lambda: run_parallel_round(tasks, controller, "首日"),
                     release_collection) as (thread, outcome):
            assert ready.wait(timeout=5)
            assert player._pending_day1_bid is None
            controller.offer_command("p")
            release_collection.set()
            assert gate.wait(timeout=5)
            assert player._pending_day1_bid is None
            assert player.messages == history
            assert "result" not in outcome
            assert any("全部成功，确认后继续本轮" in message for message in announcements)
            assert not any("补跑" in message for message in announcements)
            controller.offer_command("")
            thread.join(timeout=5)
            assert outcome.get("result") == {"Alpha": (2, 6)}
            assert len(task_runner.calls) == 1
            assert bid_calls == [1]

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
            assert gate.wait(timeout=5)
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
            returns = [p for p in parts if isinstance(p, ToolReturnPart)]
            assert len(calls) == len(returns) == 1
            assert calls[0].args_as_dict()["bid"] == 2
            assert calls[0].tool_call_id == returns[0].tool_call_id

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

    def test_pause_keeps_collected_and_inflight_success(self, monkeypatch):
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
                # yield 返回时控制器已执行 results[key] = future.result()。
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
            assert len(bravo.messages) > 1
            bravo_snapshot = deepcopy(bravo.messages)

            controller.offer_command("")
            thread.join(timeout=5)
            assert outcome.get("result") == {"Alpha": (1, None), "Bravo": (2, None)}
            assert len(alpha_chat.calls) == 1
            assert len(bravo_calls) == 1
            assert alpha.messages == alpha_snapshot
            assert bravo.messages == bravo_snapshot
            parts = [p for m in bravo.messages for p in m.parts]
            calls = [p for p in parts if isinstance(p, ToolCallPart)]
            returns = [p for p in parts if isinstance(p, ToolReturnPart)]
            assert len(calls) == len(returns) == 1
            assert calls[0].args_as_dict()["bid"] == 2
            assert calls[0].tool_call_id == returns[0].tool_call_id
