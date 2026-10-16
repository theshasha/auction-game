"""玩家持有唯一规范历史；任务成功后一次提交消息与首日缓存。"""

from __future__ import annotations

from pathlib import Path

from pydantic_ai.messages import ModelMessage, ModelRequest, SystemPromptPart

import prompts
from agent_execution import CallFailedError
from agent_log import AgentLog
from agent_tasks import TaskRunner, render_task_result
from game_rules import GameSettings
from models import Observation
from players import Player


class AIPlayer(Player):
    """任务使用临时历史；失败保留原状态，轮级检查点恢复消息及缓存。"""

    def __init__(self, name: str, persona_title: str, persona_text: str,
                 predictions: list[tuple[str, int]], all_names: list[str],
                 task_runner: TaskRunner, log_path: Path | None = None, *, game: GameSettings):
        super().__init__(name)
        self.game = game
        self.messages: list[ModelMessage] = [ModelRequest(parts=[SystemPromptPart(
            prompts.build_system_prompt(name, persona_title, persona_text, predictions, all_names, game)
        )])]
        self.task_runner = task_runner
        self.log_path = log_path
        self.log = AgentLog(name, log_path, result_renderer=render_task_result)
        self._pending_day1_bid: tuple[int, int | None] | None = None

    def checkpoint(self) -> tuple[int, tuple[int, int | None] | None]:
        """进程内快照；无需复制只增历史的前缀。"""
        return len(self.messages), self._pending_day1_bid

    def rollback(self, checkpoint) -> None:
        length, self._pending_day1_bid = checkpoint
        del self.messages[length:]

    def _task(self, task, prompt, phase, context="", *, coins=0, commit=True):
        entry = {"phase": phase, "task": task, "context": context}
        try:
            result = self.task_runner(task, prompt, self.messages, game=self.game, coins=coins)
        except CallFailedError as exc:
            self.log.failure(entry, exc)
            raise
        self.log.success(entry, result)
        if commit:
            self.messages.extend(result.messages)
        return result.output

    def decide_bid(self, obs: Observation) -> tuple[int, int | None]:
        if obs.day == 1 and self._pending_day1_bid is not None:
            submission = self._pending_day1_bid
            self._pending_day1_bid = None
            return submission
        output = self._task("bid", prompts.bid_request(obs), "bid",
                            f"第{len(obs.past_match_logs) + 1}局第{obs.day}天", coins=obs.my_coins)
        return output.bid, output.super_bid

    def give_speech(self, user_text: str, phase: str, context: str = "") -> str:
        """只返回公开字段，私有策略仅保留在自身规范消息及日志。"""
        return self._task("speech", user_text, phase, context).speech

    def plan_strategy(self, user_text: str, phase: str, context: str = "") -> None:
        """成功替换首日缓存；失败保留调用前历史与缓存。"""
        output = self._task("plan", user_text, phase, context, coins=self.game.initial_coins)
        self._pending_day1_bid = output.bid, output.super_bid

    def final_summary(self, user_text: str, context: str = "") -> str:
        """读取完整玩家历史快照，只落日志；总结请求及结果均不回写。"""
        return self._task("summary", user_text, "final_summary", context, commit=False)
