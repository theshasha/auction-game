"""原始任务留审计，已完成任务投影为普通对话；局末记忆替换游戏上下文。"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel
from pydantic_ai.messages import (
    ModelMessage, ModelRequest, ModelResponse, SystemPromptPart, TextPart, UserPromptPart,
)

from auction_game.ai import prompts
from auction_game.ai.agent_execution import CallFailedError
from auction_game.reporting.agent_log import AgentLog
from auction_game.reporting.tool_records import tool_task
from auction_game.ai.agent_tasks import TaskRunner, render_task_result
from auction_game.game.game_rules import GameSettings
from auction_game.game.models import Observation
from auction_game.game.players import Player


class AIPlayer(Player):
    """日志成功才提交；快照同时恢复当前上下文、全场回顾、记忆与缓存。"""

    def __init__(self, name: str, persona_title: str, persona_text: str,
                 predictions: list[tuple[str, int]], all_names: list[str],
                 task_runner: TaskRunner, log_path: Path | None = None, *, game: GameSettings,
                 readable_path: Path | None = None, capture_path: Path | None = None,
                 tool_writer=None, player_number=None):
        super().__init__(name)
        self.game = game
        self.messages: list[ModelMessage] = [ModelRequest(parts=[SystemPromptPart(
            prompts.build_system_prompt(name, persona_title, persona_text, predictions, all_names, game)
        )])]
        self.review_messages = list(self.messages)
        self.memory = ""
        self.task_runner = task_runner
        self.log_path = log_path
        self.log = AgentLog(name, log_path, readable_path=readable_path, capture_path=capture_path,
                            result_renderer=render_task_result)
        self._pending_day1_bid: tuple[int, int | None] | None = None
        self.tool_writer = tool_writer
        self.player_number = player_number

    def checkpoint(self):
        """消息只追加或整体替换，不原地改写；列表快照可恢复跨局重置。"""
        return list(self.messages), list(self.review_messages), self.memory, self._pending_day1_bid

    def rollback(self, checkpoint) -> None:
        messages, review, self.memory, self._pending_day1_bid = checkpoint
        self.messages = list(messages)
        self.review_messages = list(review)

    def _task(self, task, prompt, phase, context="", *, coins=0, commit=True, reset=False):
        entry = self.log.begin({"phase": phase, "task": task, "context": context})
        if self.tool_writer is not None:
            entry["tool_definitions"] = []
        try:
            with tool_task(self.tool_writer, self.name, self.player_number, entry):
                history = self.messages if commit else self.review_messages
                result = self.task_runner(task, prompt, history, game=self.game, coins=coins)
        except CallFailedError as exc:
            self.log.failure(entry, exc)
            raise
        self.log.success(entry, result)
        if commit:
            # 不重放原始 response/tool：三协议均只接收任务提示及合法业务结果，
            # 无 thinking、签名、加密 reasoning、response ID 或 provider 元数据。
            # 任务内部的纠错与工具配对仍完整交给 SDK；原始消息只写审计。
            projection = [ModelRequest(parts=[UserPromptPart(part.content)])
                          for message in result.messages for part in message.parts
                          if isinstance(part, UserPromptPart)]
            output = result.output.model_dump() if isinstance(result.output, BaseModel) else result.output
            text = json.dumps(output, ensure_ascii=False) if isinstance(output, dict) else output
            projection.append(ModelResponse(parts=[TextPart(f"已完成任务的有效结果：\n{text}")]))
            self.review_messages.extend(projection)
            if reset:
                self.memory = result.output.memory
                self.messages = [self.messages[0], ModelRequest(parts=[UserPromptPart(
                    f"【累计私有记忆，仅自己可见】\n{self.memory}")])]
                self._pending_day1_bid = None
            else:
                self.messages.extend(projection)
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

    def save_memory(self, user_text: str, phase: str, context: str = "") -> None:
        """全部公开发言公布后整理；日志成功才替换记忆并清空历局对话。"""
        self._task("memory", user_text, phase, context, reset=True)

    def final_summary(self, user_text: str, context: str = "") -> str:
        """只为观众读取全场去 thinking 业务回顾；请求及结果均不回写。"""
        return self._task("summary", user_text, "final_summary", context, commit=False)
