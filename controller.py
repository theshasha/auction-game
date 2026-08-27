"""运行控制：连续/单步模式随时切换，暂停回滚本轮所有对话。

单步粒度 = 一次 LLM 调用。一轮 = 一次并行收集（某天 6 人出价 / 某轮 6 人发言）。
暂停时取消未完成的调用、丢弃本轮已完成但未提交的结果、把所有 AIPlayer 的
对话线截断回轮开始时的 checkpoint；引擎在该轮尚未结算，重跑本轮即可。
"""

from __future__ import annotations

import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Optional

from ai_player import AIPlayer


class RoundAborted(Exception):
    """本轮被暂停抛弃：调用方应等待恢复后重跑本轮（游戏状态未被污染）。"""


class TournamentAborted(Exception):
    """用户中止整场比赛。"""


@dataclass
class RoundTask:
    key: str                  # 结果字典的键（通常是玩家名）
    description: str          # 单步模式下展示的说明，如"Alpha 第2局第1天出价"
    player: Any               # 用于 checkpoint/rollback（非 AIPlayer 则跳过）
    fn: Callable[[], Any]     # 实际执行的一次 LLM 调用（或 Bot 决策）


class Controller:
    """模式状态机。stdin 命令线程通过 offer_command 注入命令，测试可直接调用。

    命令：回车=单步推进一次调用；c=连续；s=单步；p=暂停（回滚本轮）；q=中止比赛。
    """

    def __init__(self, mode: str = "step", announce: Callable[[str], None] = print):
        self._mode = mode              # "step" | "continuous"
        self._pause_requested = False
        self._quit_requested = False
        self._gate = threading.Event()  # 单步模式下放行一次调用
        self._lock = threading.Lock()
        self._announce = announce
        self._reader_thread: Optional[threading.Thread] = None

    # ---- 命令入口 ----

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
            elif command == "p":
                self._pause_requested = True
                self._mode = "step"    # 恢复后从单步开始，等用户确认
                self._gate.set()       # 唤醒正在等待的调用，让它们看到暂停标志
                self._announce("[控制] 暂停：本轮对话将被抛弃并回滚")
            elif command == "q":
                self._quit_requested = True
                self._gate.set()

    def start_stdin_reader(self) -> None:
        def read_loop() -> None:
            for line in sys.stdin:
                self.offer_command(line.strip())

        self._reader_thread = threading.Thread(target=read_loop, daemon=True)
        self._reader_thread.start()

    # ---- 调用闸门 ----

    def wait_for_turn(self, description: str) -> None:
        """每次 LLM 调用前必须经过的闸门。"""
        if self._quit_requested:
            raise TournamentAborted
        if self._pause_requested:
            raise RoundAborted
        if self._mode == "continuous":
            return
        self._announce(f"[单步] 下一步：{description}（回车执行 / c=连续 / p=暂停 / q=中止）")
        self._gate.wait()
        self._gate.clear()
        if self._quit_requested:
            raise TournamentAborted
        if self._pause_requested:
            raise RoundAborted

    def clear_pause(self) -> None:
        self._pause_requested = False


def run_parallel_round(tasks: list[RoundTask], controller: Controller) -> dict[str, Any]:
    """执行一轮并行收集。暂停时回滚所有 AIPlayer 对话线并抛 RoundAborted。

    连续模式：6 个任务提交到线程池并发执行；收集结果时若发现暂停请求，
    已完成的结果也一并丢弃（未提交）。
    单步模式：退化为逐个确认执行。
    """
    checkpoints = [
        (task.player, task.player.checkpoint())
        for task in tasks
        if isinstance(task.player, AIPlayer)
    ]

    def rollback_all() -> None:
        for player, checkpoint in checkpoints:
            player.rollback(checkpoint)

    try:
        if controller._mode == "step":
            results = {}
            for task in tasks:
                controller.wait_for_turn(task.description)
                results[task.key] = task.fn()
            return results

        for task in tasks:
            controller.wait_for_turn(task.description)  # 连续模式下直接放行
        with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
            futures = {task.key: pool.submit(task.fn) for task in tasks}
            results = {key: future.result() for key, future in futures.items()}
        if controller._pause_requested:  # 本轮进行中收到暂停：结果不提交
            raise RoundAborted
        return results
    except (RoundAborted, TournamentAborted):
        rollback_all()
        raise
