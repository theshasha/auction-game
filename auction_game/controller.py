"""轮级运行控制：有界派发，暂停排空在途任务，成功结果只提交一次。

暂停期间任务内部已有的网络重试和输出纠错仍可能继续；控制器不取消 HTTP。
服务/输出失败恢复检查点，程序错误原样传播。q 排空后回滚整轮并中止。
"""

from __future__ import annotations

import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Callable, Optional, Protocol

from auction_game.ai.agent_execution import CallFailedError


class TournamentAborted(Exception):
    """用户中止整场比赛。"""


class CheckpointParticipant(Protocol):
    """状态由参与者解释，控制器只保存并原样交还快照。"""

    def checkpoint(self) -> Any: ...

    def rollback(self, checkpoint: Any) -> None: ...


@dataclass
class RoundTask:
    key: str
    fn: Callable[[], Any]
    participant: CheckpointParticipant | None = None


class Controller:
    """stdin 和派发共享同步边界；恢复意图保留到在途任务全部收集之后。"""

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
        self._inflight = 0
        self._announce = announce
        self._reader_thread: Optional[threading.Thread] = None

    def request_pause(self, reason: str) -> None:
        """重复暂停幂等；停止新派发，保留在途任务最终通过校验的结果。"""
        with self._changed:
            if self._pause_requested:
                return
            self._pause_requested = True
            self._resume_requested = False
            self._mode = "step"
            inflight = self._inflight
            self._gate.set()
            self._changed.notify_all()
        self._announce(f"[暂停] {reason}：正在暂停，等待 {inflight} 项在途任务"
                       "（任务内部重试可能继续；回车/c=收集后继续 / q=中止）")

    def offer_command(self, command: str) -> None:
        if command == "p":
            self.request_pause("用户请求暂停")
            return
        message = None
        with self._changed:
            if command in ("", "c"):
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
                self._quit_requested = True
                self._gate.set()
            self._changed.notify_all()
        if message:
            self._announce(message)

    def start_stdin_reader(self) -> None:
        def read_loop() -> None:
            for line in sys.stdin:
                self.offer_command(line.strip())

        self._reader_thread = threading.Thread(target=read_loop, daemon=True)
        self._reader_thread.start()

    def wait_for_turn(self, label: str, *, notices: tuple[str, ...] = (),
                      require_confirmation: bool = False) -> None:
        """先准备闸门再公告，保留公告后的输入；执行期间误按回车不预放行。"""
        with self._lock:
            if self._quit_requested:
                raise TournamentAborted
            if require_confirmation:
                # 未知失败发生前的快速恢复不替代失败后的重试确认。
                self._mode = "step"
                self._resume_requested = False
            wait = True
            if self._pause_requested and self._resume_requested:
                self._pause_requested = self._resume_requested = False
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
        self._announce(f"[单步] 下一步：{label}（回车执行 / c=连续 / p=暂停 / q=中止）")
        while True:
            self._gate.wait()
            with self._lock:
                self._gate.clear()
                if self._quit_requested:
                    raise TournamentAborted
                if self._pause_requested and not self._resume_requested:
                    continue
                self._pause_requested = self._resume_requested = False
                return


def run_parallel_round(tasks: list[RoundTask], controller: Controller, label: str) -> dict[str, Any]:
    """同一轮输入由调用方预先固定；全部结果齐全且暂停确认后才返回。

    最多派发 max_concurrency 项，不把整轮排进线程池。暂停事件与派发共锁，
    即使 p 后立即恢复，也先排空和收集这一批，再决定补跑或提交。
    """
    checkpoints = {task.key: task.participant.checkpoint()
                   for task in tasks if task.participant is not None}

    def rollback(task: RoundTask) -> None:
        if task.key in checkpoints:
            task.participant.rollback(checkpoints[task.key])

    def notify_done(_future) -> None:
        with controller._changed:
            controller._changed.notify_all()

    results: dict[str, Any] = {}
    pending = list(tasks)
    if not pending:
        return results
    gate_label = label
    try:
        controller.wait_for_turn(gate_label)
        while True:
            if not pending:
                with controller._lock:
                    if controller._quit_requested:
                        raise TournamentAborted
                    if not controller._pause_requested:
                        return results
                controller.wait_for_turn(f"{label}（全部成功，继续本轮）")
                continue
            failed: dict[str, str] = {}
            unstarted = list(pending)
            active = {}
            with ThreadPoolExecutor(max_workers=min(controller.max_concurrency, len(pending))) as pool:
                while unstarted or active:
                    with controller._changed:
                        # submit 与暂停命令在同一锁下，不会在暂停后继续补队列。
                        while (unstarted and len(active) < controller.max_concurrency
                               and not controller._pause_requested and not controller._quit_requested):
                            task = unstarted.pop(0)
                            future = pool.submit(task.fn)
                            active[future] = task
                            controller._inflight = len(active)
                            future.add_done_callback(notify_done)
                        done = [future for future in active if future.done()]
                        if not done:
                            if not active:
                                break
                            controller._changed.wait()
                            continue
                    # 必须读取所有完成结果，包括暂停前已完成但尚未收集的 future。
                    for future in as_completed(done):
                        task = active.pop(future)
                        try:
                            results[task.key] = future.result()
                        except CallFailedError as exc:
                            failed[task.key] = str(exc)
                            rollback(task)
                        with controller._lock:
                            controller._inflight = len(active)

            pending = [task for task in pending if task.key not in results]
            with controller._lock:
                if controller._quit_requested:
                    raise TournamentAborted
                paused = controller._pause_requested
                if not paused and not pending:
                    return results  # 提交与暂停共享同步边界。
            notices = []
            if paused:
                notices.append(f"[暂停] 已暂停：成功 {len(results)}、失败 {len(failed)}、"
                               f"未启动 {len(unstarted)}")
            if failed:
                notices.append(f"[失败] {'、'.join(failed)}（回车仅重试失败项并执行未启动项 / q=中止比赛）")
                for detail in failed.values():
                    notices.append(f"  {detail}")
            gate_label = (f"{label}（补跑 {len(pending)} 项）" if pending
                          else f"{label}（全部成功，继续本轮）")
            controller.wait_for_turn(gate_label, notices=tuple(notices),
                                     require_confirmation=bool(failed))
    except TournamentAborted:
        for task in tasks:
            rollback(task)
        raise
    finally:
        with controller._lock:
            controller._inflight = 0
