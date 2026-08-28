"""运行控制：连续/单步模式随时切换，单步粒度 = 一轮并发收集。

一轮 = 一次并行收集（某天 6 人出价 / 某轮 6 人发言）。单步模式下整轮开始前
闸门一次，回车放行全部任务并发执行；全部任务有定论后才进入下一轮。
中途暂停：等在途调用结束，丢弃暂停后完成的结果并回滚对应玩家的对话线，
已完成的结果保留；恢复后仅补跑未完成项。
连续两次调用失败（CallFailedError）：打印失败名单，回车仅重跑失败的 AI，
可反复直到本轮齐全。
"""

from __future__ import annotations

import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Callable, Optional

from ai_player import AIPlayer, CallFailedError


class TournamentAborted(Exception):
    """用户中止整场比赛。"""


@dataclass
class RoundTask:
    key: str                  # 结果字典的键（通常是玩家名）
    player: Any               # 用于 checkpoint/rollback（非 AIPlayer 则跳过）
    fn: Callable[[], Any]     # 实际执行的一次 LLM 调用（或 Bot 决策）


class Controller:
    """模式状态机。stdin 命令线程通过 offer_command 注入命令，测试可直接调用。

    命令：回车=推进一轮；c=连续；s=单步；p=暂停（保留已完成，恢复后仅补跑未完成项）；
    q=中止比赛。
    """

    def __init__(self, mode: str = "step", announce: Callable[[str], None] = print):
        self._mode = mode              # "step" | "continuous"
        self._pause_requested = False
        self._quit_requested = False
        self._gate = threading.Event()  # 单步模式下放行一轮
        self._lock = threading.Lock()
        self._announce = announce
        self._reader_thread: Optional[threading.Thread] = None

    # ---- 命令入口 ----

    def request_pause(self, reason: str) -> None:
        """请求暂停：本轮已完成的结果保留，未完成项回滚后切单步模式等用户确认。"""
        with self._lock:
            self._pause_requested = True
            self._mode = "step"
            self._gate.set()  # 唤醒可能正在等待的闸门，由其消费暂停标志
        self._announce(f"[暂停] {reason}（回车继续，仅补跑未完成项 / q=中止比赛）")

    def offer_command(self, command: str) -> None:
        with self._lock:
            if command == "":
                self._pause_requested = False  # 回车=继续推进，同时视为恢复
                self._gate.set()
            elif command == "c":
                self._mode = "continuous"
                self._pause_requested = False
                self._gate.set()
                self._announce("[控制] 切换到连续模式")
            elif command == "s":
                self._mode = "step"
                self._announce("[控制] 切换到单步模式")
            elif command == "q":
                self._quit_requested = True
                self._gate.set()
        if command == "p":
            self.request_pause("用户请求暂停")

    def start_stdin_reader(self) -> None:
        def read_loop() -> None:
            for line in sys.stdin:
                self.offer_command(line.strip())

        self._reader_thread = threading.Thread(target=read_loop, daemon=True)
        self._reader_thread.start()

    # ---- 轮次闸门 ----

    def wait_for_turn(self, label: str) -> None:
        """每轮开始前经过的闸门：单步模式下等回车放行整轮，连续模式直接通过。

        残留的暂停标志在这里被消费：只意味着切回单步等确认；消费时必须同时清掉
        request_pause 残留的闸门，否则恢复后不等待用户确认就空转放行。
        """
        if self._quit_requested:
            raise TournamentAborted
        while True:
            if self._pause_requested:
                with self._lock:
                    self._pause_requested = False
                    self._mode = "step"
                    self._gate.clear()
            if self._mode == "continuous":
                return
            self._announce(f"[单步] 下一步：{label}（回车执行 / c=连续 / p=暂停 / q=中止）")
            self._gate.wait()
            self._gate.clear()
            if self._quit_requested:
                raise TournamentAborted
            if not self._pause_requested:
                return  # 回车放行；若是 c 切连续，从下一轮起生效
            # 被暂停唤醒而非回车放行：回到循环顶部消费标志后重新等待


def run_parallel_round(tasks: list[RoundTask], controller: Controller, label: str) -> dict[str, Any]:
    """执行一轮并发收集，全部任务成功后才返回 {key: 结果}。

    单步模式：整轮只闸门一次（label 为轮次说明，如"第1局第1天出价"）。
    双失败（CallFailedError）：announce 失败名单与原因，切单步模式，
    回车仅重跑失败项；其他异常视为真实 bug，直接向上传播不重试。
    中途暂停：在途 HTTP 调用无法取消，等线程池排空后丢弃暂停后完成的结果
    并回滚对应玩家（避免僵尸线程污染对话线），已完成的结果保留。
    只有 TournamentAborted 会向上抛。
    """
    checkpoints = {
        task.key: task.player.checkpoint()
        for task in tasks
        if isinstance(task.player, AIPlayer)
    }

    def rollback(task: RoundTask) -> None:
        checkpoint = checkpoints.get(task.key)
        if checkpoint is not None:
            task.player.rollback(checkpoint)

    results: dict[str, Any] = {}
    pending = list(tasks)
    first_pass = True
    while pending:
        if controller._quit_requested:
            for task in tasks:
                rollback(task)
            raise TournamentAborted
        gate_label = label if first_pass else f"{label}（补跑 {len(pending)} 项）"
        controller.wait_for_turn(gate_label)
        first_pass = False

        collected: dict[str, Any] = {}
        failed: dict[str, str] = {}
        paused = False
        with ThreadPoolExecutor(max_workers=len(pending)) as pool:
            future_to_task = {pool.submit(task.fn): task for task in pending}
            for future in as_completed(future_to_task):
                task = future_to_task[future]
                if controller._pause_requested:
                    paused = True
                if paused:
                    continue  # 暂停后完成的调用：结果不提交，对应玩家稍后回滚
                try:
                    collected[task.key] = future.result()
                except CallFailedError as exc:
                    failed[task.key] = str(exc)
        # with 退出即排空线程池，以下不存在并发写
        results.update(collected)
        pending = [task for task in pending if task.key not in collected]
        for task in pending:
            rollback(task)  # 双失败项的对话线本已处于 checkpoint 状态，此处幂等

        if controller._quit_requested:
            for task in tasks:
                rollback(task)
            raise TournamentAborted
        if paused:
            continue  # request_pause 已切单步并提示，回到闸门等确认后补跑
        if failed:
            controller._mode = "step"  # 失败重跑必须经用户确认，连续模式也不例外
            names = "、".join(failed)
            controller._announce(f"[失败] {names}（回车仅重跑这些 AI / q=中止比赛）")
            for detail in failed.values():
                controller._announce(f"  {detail}")
        elif not pending:
            return results
    return results
