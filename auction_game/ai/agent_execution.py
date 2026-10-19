"""执行模型任务：复制输入历史，管理客户端，保留失败记录。"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass

import httpx2 as http
from pydantic_ai import Agent, capture_run_messages
from pydantic_ai.exceptions import ModelAPIError, UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.messages import ModelMessage, ModelResponse

from auction_game.ai.llm_client import IncompleteStream, ModelFactory


@dataclass
class ExecutionResult:
    output: object
    messages: list[ModelMessage]
    request_errors: list[dict]


class CallFailedError(Exception):
    """模型调用失败；messages 是本任务的原始往返，只写进日志。"""

    def __init__(self, message: str, *, messages=(), request_errors=()):
        super().__init__(message)
        self.messages = list(messages)
        self.request_errors = list(request_errors)


class OutputFailedError(CallFailedError):
    """纠错后输出仍无效，或请求次数用完。"""


async def _drain_events(ctx, events):
    async for _ in events:
        pass


class AgentExecutor:
    """每个任务使用独立事件循环和客户端，并在开始前深拷贝输入历史。"""

    def __init__(self, factory: ModelFactory):
        self.factory = factory

    def __call__(self, agent: Agent, prompt: str, history: list[ModelMessage], *,
                 deps=None, usage_limits=None) -> ExecutionResult:
        return asyncio.run(self._run(agent, prompt, deepcopy(history), deps, usage_limits))

    async def _run(self, agent, prompt, history, deps, usage_limits):
        async with self.factory.open() as model:
            with capture_run_messages() as captured:
                try:
                    result = await agent.run(prompt, model=model, message_history=history, deps=deps,
                                             model_settings=self.factory.settings,
                                             event_stream_handler=_drain_events, usage_limits=usage_limits)
                except (ModelAPIError, UnexpectedModelBehavior, UsageLimitExceeded, IncompleteStream,
                        http.TransportError) as exc:
                    # SDK 会合并相邻请求；只保留本次 run_id 的记录，跳过中断回复的占位消息。
                    run_id = captured[-1].run_id if captured else None
                    audit = [m for m in captured if m.run_id == run_id
                             and not (isinstance(m, ModelResponse) and m.state == "interrupted")]
                    error_type = OutputFailedError if isinstance(exc, (UnexpectedModelBehavior, UsageLimitExceeded)) else CallFailedError
                    raise error_type(f"{type(exc).__name__}: {exc}", messages=audit,
                                      request_errors=model.request_errors) from exc
            return ExecutionResult(result.output, result.new_messages(), model.request_errors)
