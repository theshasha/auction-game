"""保存原始任务日志，将已完成任务的结果提供给模型；局末用累计记忆替换旧对话。"""

from __future__ import annotations

from copy import deepcopy

from pydantic_ai.messages import (
    ModelMessage, ModelRequest, ModelResponse, SystemPromptPart, UserPromptPart,
    ThinkingPart, TextPart,
)

from auction_game.agent import prompts
from auction_game.llm.execution import CallFailedError, TaskCancelledError
from auction_game.agent.log import AgentLog
from auction_game.agent.tasks import (TASKS, BidOutput, MemoryOutput, SpeechOutput, SummaryOutput, Task,
                                      TaskOutput, TaskRunner)
from auction_game.agent.text_output import LetterNames
from auction_game.game.settings import GameSettings
from auction_game.game.models import Observation


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


class AIPlayer:
    """日志成功才提交；快照同时恢复当前上下文、全场回顾和记忆。"""

    def __init__(self, name: str, system_prompt: str, letter_names: LetterNames,
                 task_runner: TaskRunner, log: AgentLog, *, game: GameSettings):
        """系统提示由 prompts.build_system_prompt 按阵容拼好，放在两份对话的第一条。"""
        self.name = name
        self.game = game
        self.messages: list[ModelMessage] = [ModelRequest(parts=[SystemPromptPart(system_prompt)])]
        self.review_messages = list(self.messages)
        self.memory = ""
        self.task_runner = task_runner
        self.letter_names = letter_names
        self.log = log
        self.log.system_prompt(system_prompt)

    def checkpoint(self):
        """消息只追加或整体替换，浅拷贝列表就能恢复，包括局末重置。"""
        return list(self.messages), list(self.review_messages), self.memory

    def rollback(self, checkpoint) -> None:
        messages, review, self.memory = checkpoint
        self.messages = list(messages)
        self.review_messages = list(review)

    def _task(self, name: Task, prompt: str, title: str, context: str | None = None, *,
              coins: int = 0, letters: bool = True) -> TaskOutput:
        """title 是这一轮的标题，用作私人日志的小节标题；context 默认同 title。letters 为假时这次不能写信。"""
        spec = TASKS[name]
        entry = {"title": title, "task": name, "context": title if context is None else context}
        if spec.history == "review":
            entry["history_note"] = "这次请求带的是从开局到现在的全部对话，不受局末记忆重置影响；结果不写回对话历史。"
        history = self.review_messages if spec.history == "review" else self.messages
        try:
            result = self.task_runner(spec, prompt, history, game=self.game, coins=coins,
                                      letters=self.letter_names if spec.can_write_letters and letters else None)
        except CallFailedError as exc:
            self.log.failure(entry, exc)
            raise
        except TaskCancelledError as exc:
            self.log.cancelled(entry, exc)
            raise
        if spec.history == "reset":
            memory_message = f"{MEMORY_HEADER}\n{result.output.memory}"
            entry["history_reset"] = memory_message
            entry["history_note"] = f"此后对话从系统提示和一条消息重新开始，消息内容是“{MEMORY_HEADER}”加上面的结果。"
        self.log.success(entry, result)
        if spec.history != "review":
            # 日志保留原始响应；下一任务沿用去推理后的原生对话与纠错往返。
            projection = completed_history(result.messages)
            self.review_messages.extend(projection)
            if spec.history == "reset":
                self.memory = result.output.memory
                self.messages = [self.messages[0], ModelRequest(parts=[UserPromptPart(memory_message)])]
            else:
                self.messages.extend(projection)
        return result.output

    def bid(self, obs: Observation, received: dict[str, str], title: str) -> BidOutput:
        """received 是收到的信，写信人到正文。出价请求从第 2 天起发出，第 1 天的出价由本局规划交出。"""
        # 本局规划请求里有完整格式，整理记忆前一直留在对话里，出价请求只附一句提示。
        return self._task("bid", prompts.bid_request(obs, received=received), title,
                          f"第 {obs.match_index} 局第 {obs.day} 天", coins=obs.my_coins)

    def give_speech(self, user_text: str, title: str, *, letters: bool = True) -> SpeechOutput:
        """公开发言只有 speech 给其他玩家看，私有策略只留在自己的对话历史和日志里。最终感言不能写信。"""
        return self._task("speech", user_text, title, letters=letters)

    def plan_strategy(self, user_text: str, title: str) -> BidOutput:
        """规划同时交出本局第 1 天的出价。"""
        return self._task("plan", user_text, title, coins=self.game.initial_coins)

    def save_memory(self, user_text: str, title: str) -> MemoryOutput:
        """全部公开发言公布后整理；日志成功才替换记忆并清空历局对话。"""
        return self._task("memory", user_text, title)

    def final_summary(self, user_text: str, title: str) -> SummaryOutput:
        """用开局以来的全部对话写观众总结；请求和结果都不写回历史。"""
        return self._task("summary", user_text, title)
