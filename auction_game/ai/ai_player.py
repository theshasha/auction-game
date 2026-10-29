"""保存原始任务日志，将已完成任务的结果提供给模型；局末用累计记忆替换旧对话。"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from pydantic_ai.messages import (
    ModelMessage, ModelRequest, ModelResponse, SystemPromptPart, UserPromptPart,
    ThinkingPart, TextPart,
)

from auction_game.ai import prompts
from auction_game.ai.agent_execution import CallFailedError, TaskCancelledError
from auction_game.reporting.agent_log import AgentLog
from auction_game.ai.agent_tasks import TaskRunner
from auction_game.ai.text_output import LetterNames
from auction_game.game.game_rules import GameSettings
from auction_game.game.models import Observation
from auction_game.game.players import Player


MEMORY_HEADER = "【累计私有记忆，仅自己可见】"


def completed_history(messages: list[ModelMessage]) -> list[ModelMessage]:
    """复制已完成对话；去掉推理和服务端引用，保留 Responses 文本的 phase。"""
    history = deepcopy(messages)
    for message in history:
        message.metadata = None
        if isinstance(message, ModelResponse):
            message.provider_response_id = None
            message.provider_details = None
            message.failed_attempts = None
            message.workspace_ref = None
            message.parts = [part for part in message.parts if not isinstance(part, ThinkingPart)]
            for part in message.parts:
                if isinstance(part, TextPart):
                    part.id = None
                    part.provider_details = ({"phase": part.provider_details["phase"]}
                                             if part.provider_details and "phase" in part.provider_details
                                             else None)
    return [message for message in history if message.parts]


class AIPlayer(Player):
    """日志成功才提交；快照同时恢复当前上下文、全场回顾、记忆与缓存。"""

    def __init__(self, name: str, persona_title: str, persona_text: str,
                 predictions: list[tuple[str, int]], all_names: list[str],
                 task_runner: TaskRunner, log_path: Path, *, game: GameSettings,
                 readable_path: Path,
                 enable_predictions: bool = True, enable_personas: bool = True,
                 writing_style: str | None = None):
        super().__init__(name)
        self.game = game
        system_prompt = prompts.build_system_prompt(
            name, persona_title, persona_text, predictions, all_names, game,
            enable_predictions=enable_predictions, enable_personas=enable_personas,
            writing_style=writing_style)
        self.messages: list[ModelMessage] = [ModelRequest(parts=[SystemPromptPart(system_prompt)])]
        self.review_messages = list(self.messages)
        self.memory = ""
        self.task_runner = task_runner
        self.letter_names = LetterNames(name, tuple(other for other in all_names if other != name))
        self.log = AgentLog(name, log_path, readable_path=readable_path)
        self.log.system_prompt(system_prompt)
        self._pending_day1_bid: tuple[int, int | None] | None = None

    def checkpoint(self):
        """消息只追加或整体替换，浅拷贝列表就能恢复，包括局末重置。"""
        return list(self.messages), list(self.review_messages), self.memory, self._pending_day1_bid

    def rollback(self, checkpoint) -> None:
        messages, review, self.memory, self._pending_day1_bid = checkpoint
        self.messages = list(messages)
        self.review_messages = list(review)

    def _task(self, task, prompt, phase, context="", *, coins=0, commit=True, reset=False,
              letters=None, brief_format=False):
        entry = {"phase": phase, "task": task, "context": context}
        if not commit:
            entry["history_note"] = "这次请求带的是从开局到现在的全部对话，不受局末记忆重置影响；结果不写回对话历史。"
        history = self.messages if commit else self.review_messages
        try:
            result = self.task_runner(task, prompt, history, game=self.game, coins=coins,
                                      letters=letters, brief_format=brief_format)
        except CallFailedError as exc:
            self.log.failure(entry, exc)
            raise
        except TaskCancelledError as exc:
            self.log.cancelled(entry, exc)
            raise
        if reset:
            memory_message = f"{MEMORY_HEADER}\n{result.output['memory']}"
            entry["history_reset"] = memory_message
            entry["history_note"] = f"此后对话从系统提示和一条消息重新开始，消息内容是“{MEMORY_HEADER}”加上面的结果。"
        self.log.success(entry, result)
        if commit:
            # 日志保留原始响应；下一任务沿用去推理后的原生对话与纠错往返。
            projection = completed_history(result.messages)
            self.review_messages.extend(projection)
            if reset:
                self.memory = result.output["memory"]
                self.messages = [self.messages[0], ModelRequest(parts=[UserPromptPart(memory_message)])]
                self._pending_day1_bid = None
            else:
                self.messages.extend(projection)
        return result.output

    def decide_bid(self, obs: Observation) -> tuple[int, int | None]:
        if obs.day == 1 and self._pending_day1_bid is not None:
            submission = self._pending_day1_bid
            self._pending_day1_bid = None
            return submission
        # 本局规划请求里有完整格式，整理记忆前一直留在对话里，第 2 天起只附一句提示。
        output = self._task("bid", prompts.bid_request(obs), "bid",
                            f"第 {obs.match_index} 局第 {obs.day} 天", coins=obs.my_coins,
                            letters=self.letter_names, brief_format=obs.day >= 2)
        return output["bid"], output["super_bid"]

    def give_speech(self, user_text: str, phase: str, context: str = "", *, letters: bool = True) -> str:
        """只返回公开发言，私有策略只留在自己的对话历史和日志里。最终感言不能写信。"""
        return self._task("speech", user_text, phase, context,
                          letters=self.letter_names if letters else None)["speech"]

    def plan_strategy(self, user_text: str, phase: str, context: str = "") -> None:
        """成功替换首日缓存；失败保留调用前历史与缓存。"""
        output = self._task("plan", user_text, phase, context, coins=self.game.initial_coins,
                            letters=self.letter_names)
        self._pending_day1_bid = output["bid"], output["super_bid"]

    def save_memory(self, user_text: str, phase: str, context: str = "") -> None:
        """全部公开发言公布后整理；日志成功才替换记忆并清空历局对话。"""
        self._task("memory", user_text, phase, context, reset=True)

    def final_summary(self, user_text: str, context: str = "") -> str:
        """用开局以来的全部对话写观众总结；请求和结果都不写回历史。"""
        return self._task("summary", user_text, "final_summary", context, commit=False)
