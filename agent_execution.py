"""通用 Agent 执行边界：隔离历史，管理客户端生命周期及失败审计。"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Generic, TypeVar

import anthropic
import httpx2 as http
import openai
from pydantic_ai import Agent, capture_run_messages
from pydantic_ai.exceptions import ModelAPIError, UnexpectedModelBehavior
from pydantic_ai.messages import ModelMessage, ModelResponse

from llm_client import RequestCapture, IncompleteStream, ModelFactory

OutputT = TypeVar("OutputT")
DepsT = TypeVar("DepsT")


@dataclass
class ExecutionResult(Generic[OutputT]):
    output: OutputT
    messages: list[ModelMessage]
    request_errors: list[dict]
    requests: list[dict] = field(default_factory=list)


class CallFailedError(Exception):
    """预期服务失败；消息仅供审计，不提交调用方历史。"""

    def __init__(self, message: str, *, messages=(), request_errors=(), requests=()):
        super().__init__(message)
        self.messages = list(messages)
        self.request_errors = list(request_errors)
        self.requests = list(requests)


class OutputFailedError(CallFailedError):
    """输出无效或 Agent 的输出纠错预算耗尽。"""


async def _drain_events(ctx, events):
    async for _ in events:
        pass


class AgentExecutor:
    """每任务新循环、新客户端；只有此边界深拷贝输入历史。"""

    def __init__(self, factory: ModelFactory, *, capture_requests=False):
        self.factory = factory
        self.capture_requests = capture_requests

    def __call__(self, agent: Agent[DepsT, OutputT], prompt: str,
                 history: list[ModelMessage], *, deps: DepsT = None) -> ExecutionResult[OutputT]:
        return asyncio.run(self._run(agent, prompt, deepcopy(history), deps))

    async def _run(self, agent, prompt, history, deps):
        async with self.factory.open() as model:
            if self.capture_requests:
                model = RequestCapture(model)
            with capture_run_messages() as captured:
                try:
                    result = await agent.run(prompt, model=model, message_history=history, deps=deps,
                                             model_settings=self.factory.settings,
                                             event_stream_handler=_drain_events)
                except (ModelAPIError, UnexpectedModelBehavior, IncompleteStream,
                        http.TransportError, openai.APIError, anthropic.APIError) as exc:
                    # 库会合并相邻 request；按本次 run_id 取审计，排除失败流占位响应。
                    run_id = captured[-1].run_id if captured else None
                    audit = [m for m in captured if m.run_id == run_id
                             and not (isinstance(m, ModelResponse) and m.state == "interrupted")]
                    error_type = OutputFailedError if isinstance(exc, UnexpectedModelBehavior) else CallFailedError
                    raise error_type(f"{type(exc).__name__}: {exc}", messages=audit,
                                      request_errors=model.request_errors,
                                      requests=getattr(model, "requests", ())) from exc
            return ExecutionResult(result.output, result.new_messages(), model.request_errors,
                                   getattr(model, "requests", []))
