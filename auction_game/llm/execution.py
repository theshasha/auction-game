"""执行模型任务：复制输入历史，管理客户端，收到取消信号时中断，保留失败和取消记录。"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass

import httpx2 as http
import pydantic_ai
from pydantic_ai import Agent, capture_run_messages
from pydantic_ai.exceptions import ModelAPIError, UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.messages import ModelMessage, ModelResponse

from auction_game.llm.client import POLL_SECONDS, IncompleteStream, ModelFactory

# 终端输出由比赛自己安排；pydantic-ai 默认在第一次运行 Agent 时向终端打印一段横幅。
pydantic_ai.BANNER_ENABLED = False


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


class TaskCancelledError(Exception):
    """用户退出时中断了进行中的任务；messages 是取消前已完整收到的往返，只写进日志。"""

    def __init__(self, *, messages=(), request_errors=()):
        super().__init__("用户退出，任务已取消")
        self.messages = list(messages)
        self.request_errors = list(request_errors)


async def _drain_events(ctx, events):
    async for _ in events:
        pass


def _audit(captured: list[ModelMessage]) -> list[ModelMessage]:
    """SDK 会合并相邻请求；只保留本次 run_id 的记录，跳过中断回复的占位消息。"""
    run_id = captured[-1].run_id if captured else None
    return [m for m in captured if m.run_id == run_id
            and not (isinstance(m, ModelResponse) and m.state == "interrupted")]


async def _cancel_when_signalled(task: asyncio.Task, cancelled) -> None:
    while not cancelled.is_set():
        await asyncio.sleep(POLL_SECONDS)
    task.cancel()


class AgentExecutor:
    """每个任务使用独立事件循环和客户端，并在开始前深拷贝输入历史。"""

    def __init__(self, factory: ModelFactory):
        self.factory = factory

    def __call__(self, agent: Agent, prompt: str, history: list[ModelMessage], *,
                 deps=None, usage_limits=None) -> ExecutionResult:
        return asyncio.run(self._run(agent, prompt, deepcopy(history), deps, usage_limits))

    async def _run(self, agent, prompt, history, deps, usage_limits):
        signals = self.factory.signals
        async with self.factory.open() as model:
            with capture_run_messages() as captured:
                # 子任务复制当前上下文，capture_run_messages 仍能收到它的消息。
                run = asyncio.ensure_future(agent.run(
                    prompt, model=model, message_history=history, deps=deps,
                    model_settings=self.factory.settings,
                    event_stream_handler=_drain_events, usage_limits=usage_limits))
                watcher = (asyncio.ensure_future(_cancel_when_signalled(run, signals.cancelled))
                           if signals is not None else None)
                try:
                    result = await run
                except asyncio.CancelledError:
                    raise TaskCancelledError(messages=_audit(captured),
                                             request_errors=model.request_errors) from None
                except (ModelAPIError, UnexpectedModelBehavior, UsageLimitExceeded, IncompleteStream,
                        http.TransportError) as exc:
                    error_type = OutputFailedError if isinstance(exc, (UnexpectedModelBehavior, UsageLimitExceeded)) else CallFailedError
                    raise error_type(f"{type(exc).__name__}: {exc}", messages=_audit(captured),
                                     request_errors=model.request_errors) from exc
                finally:
                    if watcher is not None:
                        watcher.cancel()
            return ExecutionResult(result.output, result.new_messages(), model.request_errors)
