"""控制器命令、闸门，以及轮级并发执行的暂停、补跑和回滚。"""

import threading
import time
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace

import pytest

from auction_game.ai.ai_player import AIPlayer
from pydantic_ai.exceptions import ModelAPIError
from pydantic_ai.messages import TextPart
from tests.support.factories import (
    ALL_NAMES,
    TEST_GAME,
    TEST_PREDICTIONS,
    bid_reply,
    fake_task_runner,
    text_reply,
    make_ai_obs,
    task_entries,
)
from auction_game.controller import Controller, RoundTask, TournamentAborted, run_parallel_round
from auction_game import controller as controller_module
from auction_game.ai.agent_execution import CallFailedError, TaskCancelledError
from auction_game.ai.agent_tasks import TaskRunner


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


def make_named_player(name, task_runner, tmp_path):
    return AIPlayer(name, "测试人设", "测试人设正文", TEST_PREDICTIONS, ALL_NAMES, task_runner, game=TEST_GAME,
                    log_path=tmp_path / f"{name}.jsonl", readable_path=tmp_path / f"{name}.md")


@pytest.mark.parametrize("failure", [False, True], ids=["paused-success", "failed-task"])
@pytest.mark.parametrize("command", ["", "q"], ids=["resume", "quit"])
def test_scoring_waits_for_round_confirmation_and_commits_once(monkeypatch, capsys, failure, command, tmp_path):
    from auction_game.game import engine
    from auction_game.game.tournament import run_tournament
    from auction_game.reporting.run_output import print_day_result

    game = replace(TEST_GAME, daily_pool=(3,), super_hit_score=1, regular_matches=1, max_playoffs=0)
    gate = threading.Event()
    controller = Controller(mode="continuous", max_concurrency=1,
                            announce=lambda text: gate.set() if text.startswith("[单步]") else None)

    def pause_and_reply(messages):
        controller.offer_command("p")
        return bid_reply(2)

    alpha = make_named_player("Alpha", fake_task_runner([bid_reply(1, 2)]), tmp_path)
    bravo = make_named_player("Bravo", fake_task_runner(
        [ModelAPIError("offline", "unavailable"), bid_reply(2)] if failure else [pause_and_reply],
    ), tmp_path)
    for player in (alpha, bravo):
        player.game = game
    alpha.plan_strategy("首日规划", "opening_plan")
    before = [player.checkpoint() for player in (alpha, bravo)]
    settlements, published, day_reports = [], [], []
    settle = engine.settle_day

    def count_settlement(*args, **kwargs):
        settlements.append(True)
        return settle(*args, **kwargs)

    def collect(observations):
        tasks = [RoundTask(player.name, lambda p=player, o=obs: p.decide_bid(o), player)
                 for player, obs in observations]
        results = run_parallel_round(tasks, controller, "测试出价")
        return [results[player.name] for player, _ in observations]

    def report_day(log, standings, coins):
        day_reports.append((log.days[-1], standings, coins))
        print_day_result(log, standings, coins)

    monkeypatch.setattr(engine, "settle_day", count_settlement)
    with running(controller, lambda: run_tournament(
        [alpha, bravo], game, bid_collector=collect,
        on_match_end=lambda log, standings: published.append(log),
        on_day_end=report_day,
    )) as (thread, outcome):
        assert gate.wait(timeout=5)
        assert settlements == published == day_reports == []
        assert capsys.readouterr().out == ""
        assert alpha._pending_day1_bid is None
        controller.offer_command(command)
        thread.join(timeout=5)
        assert not thread.is_alive()
        if command == "q":
            assert isinstance(outcome.get("error"), TournamentAborted)
            assert settlements == published == day_reports == []
            assert capsys.readouterr().out == ""
            assert [player.checkpoint() for player in (alpha, bravo)] == before
        else:
            assert settlements == [True]
            assert len(published) == 1
            assert published[0].scores == {"Alpha": 4, "Bravo": 0}
            assert outcome["result"].final_standings[0].total_score == 4
            assert len(day_reports) == 1
            output = capsys.readouterr().out
            assert output.count("===== 第 1 局（正赛）第 1 天 =====") == 1
            assert "超级命中：Alpha +1 分" in output
        assert len(alpha.task_runner.calls) == 1
        assert len(bravo.task_runner.calls) == (2 if failure and command != "q" else 1)


@pytest.mark.parametrize("command", ["", "q"], ids=["resume-only-failed-memory", "abort-restores-context"])
def test_memory_round_keeps_successful_reset_and_can_restore_whole_round(command, tmp_path):
    gate = threading.Event()

    def announce(message):
        if message.startswith("[单步]"):
            gate.set()

    controller = Controller(mode="continuous", announce=announce)
    alpha = make_named_player("Alpha", fake_task_runner([bid_reply(1), {"memory": "alpha cumulative"}]), tmp_path)
    bravo = make_named_player("Bravo", fake_task_runner([bid_reply(2), ModelAPIError("offline", "unavailable"),
                                                        {"memory": "bravo cumulative"}]), tmp_path)
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

    def test_enter_during_round_is_discarded_in_step_mode(self):
        first_gate = threading.Event()
        second_gate = threading.Event()

        def announce(msg):
            if "第1轮" in msg:
                first_gate.set()
            elif "第2轮" in msg:
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

    def test_quit_is_acknowledged_once_and_raises_tournament_aborted(self):
        announcements = []
        controller = Controller(mode="continuous", announce=announcements.append)
        controller.offer_command("q")
        controller.offer_command("q")
        assert sum(message.startswith("[退出]") for message in announcements) == 1
        with pytest.raises(TournamentAborted):
            controller.wait_for_turn("测试调用")

    def test_unrecognized_command_is_shown_and_ignored(self):
        announcements = []
        controller = Controller(mode="continuous", announce=announcements.append)
        controller.offer_command("\x1b[Iq")
        assert len(announcements) == 1 and "'\\x1b[Iq'" in announcements[0]
        controller.wait_for_turn("测试调用")  # 未退出，连续模式直接通过

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
    def test_step_mode_gates_once_then_dispatches_bounded(self, limit):
        count = 5
        releases = [threading.Event() for _ in range(count)]
        started = [threading.Event() for _ in range(count)]
        lock = threading.Lock()
        live = 0
        peak = 0
        calls = []
        gates = []
        gate = threading.Event()

        def announce(message):
            if message.startswith("[单步]"):
                gates.append(message)
                gate.set()

        controller = Controller(mode="step", max_concurrency=limit, announce=announce)

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
            assert gate.wait(timeout=5)
            assert not any(event.is_set() for event in started)
            controller.offer_command("")
            first_batch = min(limit, count)
            assert all(event.wait(timeout=5) for event in started[:first_batch])
            assert not any(event.is_set() for event in started[first_batch:])
            for index in range(count):
                assert started[index].wait(timeout=5)
                releases[index].set()
            thread.join(timeout=5)
            assert outcome.get("result") == {str(i): i for i in range(count)}
            assert peak == first_batch
            assert len(gates) == 1
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
            # 一条是收到 p 时的提示，一条是收集结果后的汇总；重复的 p 不再提示。
            assert sum(message.startswith("[暂停]") for message in announcements) == 2

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

    @pytest.mark.parametrize("command", ["", "c"])
    def test_pause_retries_failed_and_unstarted_only_after_new_confirmation(self, command):
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
                controller.offer_command(command)  # 失败前的回车或继续命令不能确认后续重试
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
            assert any(message.startswith("[暂停]") for message in announcements)
            assert any(message.startswith("[失败]") for message in announcements)
            controller.offer_command("")
            thread.join(timeout=5)
            assert outcome.get("result") == {"0": 0, "1": 1, "2": 2}
            assert calls == [1, 2, 1]

    @pytest.mark.parametrize("paused", [False, True])
    @pytest.mark.parametrize("command", ["", "c"])
    def test_failure_notice_accepts_immediate_confirmation(self, paused, command):
        failures = 2
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

    def test_quit_cancels_inflight_rolls_back_and_does_not_dispatch_more(self):
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
                self.value = 1
                started.set()
                # 模拟执行器：只有取消信号能结束进行中的请求。
                assert controller.signals.cancelled.wait(timeout=5)
                raise TaskCancelledError()

        participant = Participant()
        tasks = [RoundTask("first", participant.run, participant),
                 RoundTask("unstarted", lambda: pytest.fail("q 后不得派发"))]
        with running(controller, lambda: run_parallel_round(tasks, controller, "中止")) as (thread, outcome):
            assert started.wait(timeout=5)
            controller.offer_command("q")
            thread.join(timeout=5)
            assert isinstance(outcome.get("error"), TournamentAborted)
            assert participant.value == 0
            assert participant.restored == [None, None]  # 取消时一次，中止整轮时一次

    def test_pause_signal_follows_pause_and_resume(self):
        controller = Controller(mode="continuous", announce=lambda _: None)
        controller.offer_command("p")
        assert controller.signals.paused.is_set() and not controller.signals.cancelled.is_set()
        controller.offer_command("")
        controller.wait_for_turn("恢复")
        assert not controller.signals.paused.is_set()

    @pytest.mark.parametrize("pause_first", [False, True], ids=["q", "p-then-q"])
    def test_quit_interrupts_real_request_and_logs_cancelled(self, pause_first, tmp_path):
        from tests.support.wire import Harness

        controller = Controller(mode="continuous", announce=lambda _: None)
        h = Harness("chat", ["hang"])
        runner = TaskRunner(replace(h.factory, signals=controller.signals))
        player = make_named_player("Alpha", runner, tmp_path)
        before = player.checkpoint()
        tasks = [RoundTask("Alpha", lambda: player.decide_bid(make_ai_obs(day=2)), player)]
        with running(controller, lambda: run_parallel_round(tasks, controller, "出价")) as (thread, outcome):
            deadline = time.monotonic() + 5
            while not h.requests and time.monotonic() < deadline:
                time.sleep(0.01)
            assert h.requests
            if pause_first:
                controller.offer_command("p")
                thread.join(timeout=0.3)
                assert thread.is_alive()  # 暂停不打断进行中的请求
            controller.offer_command("q")
            thread.join(timeout=2)
            assert not thread.is_alive()
            assert isinstance(outcome.get("error"), TournamentAborted)
        assert player.checkpoint() == before
        assert [entry["status"] for entry in task_entries(player.log.path)] == ["cancelled"]
        assert "> 已取消：用户按 q 退出比赛，请求被中断。" in player.log.readable_path.read_text(encoding="utf-8")

    def test_program_error_propagates_after_pause(self):
        controller = Controller(mode="continuous", announce=lambda _: None)

        def broken():
            controller.offer_command("p")
            raise ValueError("真实程序错误")

        with pytest.raises(ValueError, match="真实程序错误"):
            run_parallel_round([RoundTask("broken", broken)], controller, "程序错误")

    @pytest.mark.parametrize("command", ["", "q"], ids=["resume", "quit"])
    def test_failed_participant_rolls_back_and_successful_result_is_kept(self, command):
        notice = threading.Event()

        def announce(message):
            if message.startswith("[失败]"):
                notice.set()

        controller = Controller(mode="continuous", max_concurrency=1, announce=announce)

        class Counter:
            def __init__(self):
                self.value = 0
                self.calls = 0
                self.restored = []

            def checkpoint(self):
                return "start"

            def rollback(self, checkpoint):
                self.restored.append(checkpoint)
                self.value = 0

            def run(self):
                self.calls += 1
                self.value += 1
                if self.calls == 1:
                    raise CallFailedError("测试服务故障")
                return self.value

        counter = Counter()
        stateless_calls = []

        def stateless():
            stateless_calls.append(1)
            return "ready"

        tasks = [RoundTask("stateless", stateless), RoundTask("counter", counter.run, participant=counter)]
        with running(controller, lambda: run_parallel_round(tasks, controller, "通用任务")) as (thread, outcome):
            assert notice.wait(timeout=5)
            assert counter.value == 0 and counter.restored == ["start"]
            controller.offer_command(command)
            thread.join(timeout=5)
            if command == "q":
                assert isinstance(outcome.get("error"), TournamentAborted)
                assert counter.restored == ["start", "start"]
            else:
                assert outcome.get("result") == {"stateless": "ready", "counter": 1}
                assert counter.calls == 2
            assert stateless_calls == [1]

    @pytest.mark.parametrize("command", ["", "q"], ids=["resume", "quit"])
    def test_pause_keeps_consumed_plan_before_result_collection(self, monkeypatch, command, tmp_path):
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
        player = make_named_player("Alpha", task_runner, tmp_path)
        player.plan_strategy("请规划", "opening_plan")
        checkpoint = player.checkpoint()
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
            controller.offer_command(command)
            thread.join(timeout=5)
            if command == "q":
                assert isinstance(outcome.get("error"), TournamentAborted)
                assert player.checkpoint() == checkpoint
            else:
                assert outcome.get("result") == {"Alpha": (2, 6)}
                assert player._pending_day1_bid is None
            assert len(task_runner.calls) == 1
            assert bid_calls == [1]

    @pytest.mark.parametrize("failures, reason", [
        ([ModelAPIError("offline", "service unavailable")], "service unavailable"),
        (["格式不对", "还是不对"], "retries"),
    ], ids=["service", "output"])
    def test_failure_reruns_only_failed_player(self, failures, reason, tmp_path):
        gate = threading.Event()
        announcements = []

        def announce(msg):
            announcements.append(msg)
            if msg.startswith("[单步]"):
                gate.set()

        controller = Controller(mode="continuous", announce=announce)
        alpha_chat = fake_task_runner([bid_reply(1)])
        alpha = make_named_player("Alpha", alpha_chat, tmp_path)
        bravo_chat = fake_task_runner([*failures, bid_reply(2)])
        bravo = make_named_player("Bravo", bravo_chat, tmp_path)
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
            assert [p.content for p in parts if isinstance(p, TextPart)] == [text_reply(bid_reply(2))]

    @pytest.mark.parametrize("command", ["", "q"], ids=["resume", "quit"])
    def test_pause_keeps_collected_and_inflight_success(self, monkeypatch, command, tmp_path):
        gate_announced = threading.Event()

        def announce(msg):
            if msg.startswith("[单步]"):
                gate_announced.set()

        controller = Controller(mode="continuous", announce=announce)
        alpha_collected = threading.Event()
        alpha_chat = fake_task_runner([bid_reply(1)])
        alpha = make_named_player("Alpha", alpha_chat, tmp_path)

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

        bravo = make_named_player("Bravo", fake_task_runner([bravo_chat, bravo_chat]), tmp_path)
        checkpoints = [p.checkpoint() for p in (alpha, bravo)]
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

            controller.offer_command(command)
            thread.join(timeout=5)
            assert len(alpha_chat.calls) == 1
            assert len(bravo_calls) == 1
            if command == "q":
                assert isinstance(outcome.get("error"), TournamentAborted)
                assert [p.checkpoint() for p in (alpha, bravo)] == checkpoints
            else:
                assert outcome.get("result") == {"Alpha": (1, None), "Bravo": (2, None)}
                assert alpha.messages == alpha_snapshot
                assert bravo.messages == bravo_snapshot
            parts = [p for m in bravo_snapshot for p in m.parts]
            assert [p.content for p in parts if isinstance(p, TextPart)] == [text_reply(bid_reply(2))]
