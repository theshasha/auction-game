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
from auction_game.agent.tasks import (TASKS, BidOutput, DiscussionOutput, MemoryOutput, SpeechOutput, SummaryOutput,
                                      Task, TaskOutput, TaskRunner)
from auction_game.agent.text_output import BRIEF_DISCUSSION_FORMAT, LetterScope
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
    """日志成功才提交；快照同时恢复当前上下文和各局记忆。"""

    def __init__(self, name: str, system_prompt: str, letter_scope: LetterScope,
                 task_runner: TaskRunner, log: AgentLog, *, game: GameSettings):
        """系统提示由 prompts.build_system_prompt 按阵容拼好，放在对话的第一条。"""
        self.name = name
        self.game = game
        self.messages: list[ModelMessage] = [ModelRequest(parts=[SystemPromptPart(system_prompt)])]
        self.memories: list[str] = []  # 每局整理的记忆，按局序；最后一项是对话里的最新一版。
        self.task_runner = task_runner
        self.letter_scope = letter_scope
        self.log = log
        self.log.system_prompt(system_prompt)

    def checkpoint(self):
        """消息只追加或整体替换，浅拷贝列表就能恢复，包括局末重置。"""
        return list(self.messages), list(self.memories)

    def rollback(self, checkpoint) -> None:
        messages, memories = checkpoint
        self.messages = list(messages)
        self.memories = list(memories)

    def _task(self, name: Task, prompt: str, title: str, context: str | None = None, *,
              coins: int = 0, letters: bool = True, brief: str | None = None) -> TaskOutput:
        """title 是这一轮的标题，用作私人日志的小节标题；context 默认同 title。letters 为假时这次不能写信。

        brief 是只附一句格式提示时的那一句，不传就附完整格式。
        """
        spec = TASKS[name]
        entry = {"title": title, "task": name, "context": title if context is None else context}
        if spec.history == "none":
            entry["history_note"] = "这次请求接在当前对话后面，结果不写回对话历史。"
        try:
            result = self.task_runner(spec, prompt, self.messages, game=self.game, coins=coins,
                                      letters=self.letter_scope if spec.can_write_letters and letters else None,
                                      brief=brief)
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
        if spec.history == "reset":
            self.memories.append(result.output.memory)
            self.messages = [self.messages[0], ModelRequest(parts=[UserPromptPart(memory_message)])]
        elif spec.history == "append":
            # 日志保留原始响应；下一任务沿用去推理后的原生对话与纠错往返。
            self.messages.extend(completed_history(result.messages))
        return result.output

    def bid(self, obs: Observation, received: dict[str, str], title: str) -> BidOutput:
        """received 是收到的信，写信人到正文。出价请求从第 2 天起发出，第 1 天的出价由本局规划交出。"""
        # 本局规划请求里有完整格式，整理记忆前一直留在对话里，出价请求只附一句提示。
        return self._task("bid", prompts.bid_request(obs, received=received, team=self.letter_scope.team), title,
                          f"第 {obs.match_index} 局第 {obs.day} 天", coins=obs.my_coins,
                          brief=self.letter_scope.brief_bid)

    def give_speech(self, user_text: str, title: str, *, letters: bool = True) -> SpeechOutput:
        """公开发言只有 speech 给其他玩家看，私有策略只留在自己的对话历史和日志里。最终感言不能写信。"""
        return self._task("speech", user_text, title, letters=letters)

    def discuss(self, user_text: str, title: str, *, first: bool) -> DiscussionOutput:
        """开局讨论的一轮；first 是第 1 轮，附完整格式，之后的轮次只附一句提示。"""
        return self._task("discussion", user_text, title, brief=None if first else BRIEF_DISCUSSION_FORMAT)

    def plan_strategy(self, user_text: str, title: str) -> BidOutput:
        """规划同时交出本局第 1 天的出价。"""
        return self._task("plan", user_text, title, coins=self.game.initial_coins)

    def save_memory(self, user_text: str, title: str) -> MemoryOutput:
        """全部公开发言公布后整理；日志成功才替换记忆并清空历局对话。"""
        return self._task("memory", user_text, title)

    def final_summary(self, others_speeches: dict[str, str], title: str) -> SummaryOutput:
        """接着当前对话写观众总结，最新一版记忆已在对话里，请求补上之前各版；请求和结果都不写回历史。"""
        return self._task("summary", prompts.final_summary_request(others_speeches, self.memories[:-1]), title)
