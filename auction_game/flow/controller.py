"""轮级运行控制：限制同时运行的任务数，暂停时等已启动任务结束，成功结果只提交一次。

暂停后任务不再重发失败的请求，进行中的请求和输出纠错照常完成。
服务或输出失败时恢复该任务的检查点，程序错误直接抛出。q 通过取消信号马上中断已启动的任务，
把整轮恢复到开始时的状态并中止比赛；已成功的任务留在日志里，续跑时沿用。
"""

from __future__ import annotations

import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Callable

from auction_game.llm.execution import CallFailedError, TaskCancelledError
from auction_game.llm.client import RunSignals


class TournamentAborted(Exception):
    """用户中止整场比赛。"""


@dataclass
class RoundTask:
    key: str
    fn: Callable[[], Any]
    participant: Any = None  # 提供 checkpoint() 和 rollback(checkpoint)，失败或退出时据此恢复状态。


class Controller:
    """stdin 命令和任务派发共用一把锁；收到继续命令后，仍先等已启动任务结束并收集结果。"""

    def __init__(self, mode: str = "step", announce: Callable[[str], None] = print,
                 *, max_concurrency: int = 10):
        self.max_concurrency = max_concurrency
        self._mode = mode
        self._pause_requested = False
        self._resume_requested = False
        self._quit_requested = False
        self._gate = threading.Event()
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)
        self._announce = announce
        self.signals = RunSignals()  # 交给模型工厂，任务据此停止重发或中断请求

    def _set_paused(self, paused: bool) -> None:
        self._pause_requested = paused
        if paused:
            self.signals.paused.set()
        else:
            self.signals.paused.clear()

    def offer_command(self, command: str) -> None:
        message = None
        with self._changed:
            if command == "p":
                # 已暂停时重复 p 不清除恢复意图。
                if self._pause_requested:
                    return
                self._set_paused(True)
                self._resume_requested = False
                self._mode = "step"
                self._gate.set()
                message = ("[暂停] 停止新任务和失败请求的重发，等进行中的请求结束。成功结果保留。"
                           "回车或 c：等请求结束后继续；q：马上取消已启动的任务并退出比赛，已成功的结果续跑时沿用。")
            elif command in ("", "c"):
                if self._pause_requested:
                    self._resume_requested = True
                if command == "c":
                    self._mode = "continuous"
                    message = "[控制] 切换到连续模式"
                self._gate.set()
            elif command == "s":
                self._mode = "step"
                message = "[控制] 切换到单步模式"
            elif command == "q":
                if self._quit_requested:
                    return
                self._quit_requested = True
                self.signals.cancelled.set()
                self._gate.set()
                message = "[退出] 正在取消已启动的任务并退出比赛，已成功的结果续跑时沿用。"
            else:
                # 用 repr 显示终端混入的控制字符，便于判断按键是否被破坏。
                message = f"[控制] 无法识别：{command!r}"
            self._changed.notify_all()
        if message:
            self._announce(message)

    def start_stdin_reader(self) -> None:
        def read_loop() -> None:
            for line in sys.stdin:
                self.offer_command(line.strip())

        threading.Thread(target=read_loop, daemon=True).start()

    def wait_for_turn(self, label: str, *, notices: tuple[str, ...] = (),
                      require_confirmation: bool = False) -> None:
        """先进入等待状态再显示提示，提示之后的按键有效；执行期间误按的回车不算确认。"""
        with self._lock:
            if self._quit_requested:
                raise TournamentAborted
            if require_confirmation:
                # 失败前的快速恢复命令不能代替失败后的补跑确认。
                self._mode = "step"
                self._resume_requested = False
            wait = True
            if self._pause_requested and self._resume_requested:
                self._set_paused(False)
                self._resume_requested = False
                self._gate.clear()
                wait = False
            elif self._mode == "continuous" and not self._pause_requested:
                wait = False
            else:
                self._gate.clear()
        for message in notices:
            self._announce(message)
        if not wait:
            return
        self._announce(f"[单步] 下一步：{label}（回车执行 / c=连续 / p=暂停 / q=退出）")
        while True:
            self._gate.wait()
            with self._lock:
                self._gate.clear()
                if self._quit_requested:
                    raise TournamentAborted
                if self._pause_requested and not self._resume_requested:
                    continue
                self._set_paused(False)
                self._resume_requested = False
                return

    def run_round(self, tasks: list[RoundTask], label: str) -> dict[str, Any]:
        """运行一轮输入已固定的任务；全部成功，且暂停过时用户已确认继续，才返回结果。

        同时最多运行 max_concurrency 项。派发和暂停共用一把锁；即使 p 后立即恢复，
        也先等已启动任务结束并收集结果，再决定补跑或提交。
        """
        checkpoints = {task.key: task.participant.checkpoint()
                       for task in tasks if task.participant is not None}

        def rollback(task: RoundTask) -> None:
            if task.key in checkpoints:
                task.participant.rollback(checkpoints[task.key])

        def notify_done(_future) -> None:
            with self._changed:
                self._changed.notify_all()

        results: dict[str, Any] = {}
        pending = list(tasks)
        gate_label = label
        try:
            self.wait_for_turn(gate_label)
            while True:
                if not pending:
                    with self._lock:
                        if self._quit_requested:
                            raise TournamentAborted
                        if not self._pause_requested:
                            return results
                    self.wait_for_turn(f"{label}（全部成功，确认后继续）")
                    continue
                failed: dict[str, str] = {}
                unstarted = list(pending)
                active = {}
                with ThreadPoolExecutor(max_workers=min(self.max_concurrency, len(pending))) as pool:
                    while unstarted or active:
                        with self._changed:
                            # 派发和暂停命令在同一把锁下，暂停后不会再派发。
                            while (unstarted and len(active) < self.max_concurrency
                                   and not self._pause_requested and not self._quit_requested):
                                task = unstarted.pop(0)
                                future = pool.submit(task.fn)
                                active[future] = task
                                future.add_done_callback(notify_done)
                            done = [future for future in active if future.done()]
                            if not done:
                                if not active:
                                    break
                                self._changed.wait()
                                continue
                        # 收集所有已完成的结果，包括暂停前已完成但还没收集的。
                        # done 里都已完成；用 as_completed 是为了让竞态测试在收集时机插入等待。
                        for future in as_completed(done):
                            task = active.pop(future)
                            try:
                                results[task.key] = future.result()
                            except CallFailedError as exc:
                                failed[task.key] = str(exc)
                                rollback(task)
                            except TaskCancelledError:
                                rollback(task)  # 只在 q 之后发生，下面随即中止比赛

                pending = [task for task in pending if task.key not in results]
                with self._lock:
                    if self._quit_requested:
                        raise TournamentAborted
                    paused = self._pause_requested
                    if not paused and not pending:
                        return results  # 提交和暂停在同一把锁下判断。
                notices = []
                if paused:
                    notices.append(f"[暂停] 成功 {len(results)}，失败 {len(failed)}，"
                                   f"未启动 {len(unstarted)}。成功结果已保留。")
                if failed:
                    notices.append(f"[失败] {'、'.join(failed)}：回车或 c 只补跑失败和未启动任务，"
                                   "成功任务不重跑；q 退出比赛，成功的结果续跑时沿用。")
                    for detail in failed.values():
                        notices.append(f"  {detail}")
                gate_label = (f"{label}（补跑 {len(pending)} 项）" if pending
                              else f"{label}（全部成功，确认后继续）")
                self.wait_for_turn(gate_label, notices=tuple(notices),
                                   require_confirmation=bool(failed))
        except TournamentAborted:
            for task in tasks:
                rollback(task)
            raise
