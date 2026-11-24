"""各协议的模型工厂：完整流检查，请求失败后等待并重发。"""

from __future__ import annotations

import asyncio
from contextlib import ExitStack, asynccontextmanager
from dataclasses import dataclass, field
import os
import threading

from dotenv import load_dotenv
import httpx2 as http
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaRawMessageDeltaEvent
import anthropic
from openai import AsyncOpenAI
import openai
from google import genai
from google.genai.types import HttpOptions, ResourceScope
from pydantic_ai.exceptions import ModelAPIError, UnexpectedModelBehavior
from pydantic_ai.models import CompletedStreamedResponse
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.google_cloud import GoogleCloudProvider
from pydantic_ai.providers.openai import OpenAIProvider
from auction_game.paths import ENV_PATH


class IncompleteStream(RuntimeError):
    """输出截断、流为空或缺少成功结束标记。"""


# 请求失败的异常：HTTP 错误、SDK 调用错误、网络错误、截断、为空或未正常结束的流。其余异常是程序错误。
REQUEST_ERRORS = (ModelAPIError, IncompleteStream, openai.APIError, anthropic.APIError, http.TransportError)
POLL_SECONDS = 0.1
REQUEST_ATTEMPTS = 4  # 比赛每个请求首次加重发 3 次；连通检查只请求一次。


@dataclass(frozen=True)
class RunSignals:
    """控制器在线程间传给任务的信号：暂停后不再重发，取消时中断进行中的请求。"""

    paused: threading.Event = field(default_factory=threading.Event)
    cancelled: threading.Event = field(default_factory=threading.Event)


async def wait_before_retry(delay: float, signals: RunSignals | None) -> bool:
    """等待 delay 秒；期间暂停则提前返回 False。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + delay
    while (remaining := deadline - loop.time()) > 0:
        if signals is not None and signals.paused.is_set():
            return False
        await asyncio.sleep(min(POLL_SECONDS, remaining))
    return signals is None or not signals.paused.is_set()


class RequireDeltaUsage:
    """逐个转交 Anthropic 流事件；message_delta 缺 usage 时按请求失败处理。

    hub 偶尔返回没有内容、message_delta 也缺 usage 的流，SDK 解析时会抛 AttributeError。
    用普通迭代器而不是异步生成器，放弃流时不留下待回收的生成器。
    """

    def __init__(self, stream):
        self._iterator = aiter(stream)

    def __aiter__(self):
        return self

    async def __anext__(self):
        event = await anext(self._iterator)
        if isinstance(event, BetaRawMessageDeltaEvent) and event.usage is None:
            raise IncompleteStream("message_delta 缺少 usage")
        return event


class CheckedAnthropicModel(AnthropicModel):
    async def _process_streamed_response(self, response, model_request_parameters, model_settings):
        streamed = await super()._process_streamed_response(response, model_request_parameters, model_settings)
        streamed._response = RequireDeltaUsage(response)
        return streamed


class BufferedRequest(WrapperModel):
    """完整消费流并确认正常结束后才交付；请求失败后等待 retry_delay 秒重发，暂停后不再重发。

    Anthropic 公开响应只提供 stop_reason，原始 message_stop 缺失时无法单独识别。
    """

    def __init__(self, wrapped, *, attempts: int, retry_delay: float, signals: RunSignals | None):
        super().__init__(wrapped)
        self.request_errors: list[dict] = []
        self.attempts = attempts
        self.retry_delay = retry_delay
        self.signals = signals
        self._request_number = 0  # 每个任务新建包装器；重发共用同一请求序号

    @asynccontextmanager
    async def request_stream(self, messages, model_settings, model_request_parameters, run_context=None):
        self._request_number += 1
        request_number = self._request_number
        for attempt in range(1, self.attempts + 1):
            try:
                try:
                    async with self.wrapped.request_stream(
                        messages, model_settings, model_request_parameters, run_context
                    ) as stream:
                        async for _ in stream:
                            pass
                        response = stream.get()
                except UnexpectedModelBehavior as exc:
                    # SDK 收到空流或拼接不上的流事件时抛出。输出校验的同类异常在 Agent 图里抛出，不经过这里。
                    raise IncompleteStream(str(exc)) from exc
                if response.finish_reason != "stop":
                    # SDK 把多种渠道结束原因映射成同一个值，Vertex 的 OTHER 等都成了 None，要记下原始值才能区分。
                    raw = (response.provider_details or {}).get("finish_reason")
                    detail = f"渠道原始值 {raw!r}" if raw is not None else "渠道没有给出结束原因"
                    raise IncompleteStream(f"finish_reason={response.finish_reason!r}（{detail}）")
                break
            except REQUEST_ERRORS as exc:
                # pydantic-ai 把连接失败和超时都包成 ModelAPIError，记下被包的异常才分得出是哪一种。
                cause = exc.__cause__ if type(exc) is ModelAPIError and exc.__cause__ is not None else exc
                self.request_errors.append({"request": request_number, "attempt": attempt,
                                            "type": type(cause).__name__, "message": str(exc)})
                if attempt == self.attempts or not await wait_before_retry(self.retry_delay, self.signals):
                    raise
        # yield 在重试范围外：Agent 要求纠错时发新请求，不重发已完成的请求。
        yield CompletedStreamedResponse(response, model_request_parameters=model_request_parameters)


@dataclass(frozen=True)
class ModelFactory:
    """保存模型配置；open() 在调用方的事件循环里创建并关闭 SDK 和 HTTP 客户端。

    transport 用于测试注入假传输。
    """

    protocol: str
    model: str
    base_url: str
    api_key: str = field(repr=False)
    timeout: float
    max_tokens: int
    transport: http.AsyncBaseTransport | None = None
    request_attempts: int = REQUEST_ATTEMPTS
    model_settings: dict = field(default_factory=dict)
    retry_delay: float = 10
    signals: RunSignals | None = None  # 比赛由控制器提供；连通检查和离线赛不需要

    @property
    def settings(self) -> dict:
        return {**self.model_settings, "timeout": self.timeout, "max_tokens": self.max_tokens}

    @asynccontextmanager
    async def open(self):
        # SDK 使用注入的 HTTP 客户端，由外层 async with 统一关闭，构造失败时也会关闭。
        async with http.AsyncClient(transport=self.transport) as client:
            with ExitStack() as stack:
                yield BufferedRequest(self._model(client, stack), attempts=self.request_attempts,
                                      retry_delay=self.retry_delay, signals=self.signals)

    def _model(self, client: http.AsyncClient, stack: ExitStack):
        if self.protocol == "anthropic":
            sdk = AsyncAnthropic(base_url=self.base_url, api_key=self.api_key,
                                 http_client=client, max_retries=0)
            return CheckedAnthropicModel(self.model, provider=AnthropicProvider(anthropic_client=sdk))
        if self.protocol == "vertex":
            # 快速模式密钥。COLLECTION 让 SDK 把渠道地址原样当作 publishers/... 的前缀，版本、项目和区域都由渠道地址决定。
            # 不给 retry_options 时 SDK 不重发被拒的请求；不注入 httpx2 客户端时 SDK 在装了 aiohttp 的环境里改用 aiohttp，
            # 传输错误就不再是 httpx2 的。SDK 的自动续接保留，影响见 docs/usage.md 的"接入模型"。
            sdk = genai.Client(vertexai=True, api_key=self.api_key, http_options=HttpOptions(
                base_url=self.base_url, base_url_resource_scope=ResourceScope.COLLECTION,
                httpx_async_client=client))
            # SDK 总会另建一个同步客户端，外层 async with 关不到它，不关就要等垃圾回收。
            stack.callback(sdk.close)
            return GoogleModel(self.model, provider=GoogleCloudProvider(client=sdk))
        sdk = AsyncOpenAI(base_url=self.base_url, api_key=self.api_key, http_client=client, max_retries=0)
        provider = OpenAIProvider(openai_client=sdk)
        if self.protocol == "chat":
            # SDK 默认把缺结束原因的截断流当成正常结束；部分中转渠道不认 max_completion_tokens。
            return OpenAIChatModel(self.model, provider=provider, profile={
                "openai_chat_streaming_requires_finish_reason": True,
                "openai_chat_supports_max_completion_tokens": False,
            })
        return OpenAIResponsesModel(self.model, provider=provider)


class MissingEnvError(RuntimeError):
    pass


def _require_env(name: str, provider: str) -> str:
    load_dotenv(ENV_PATH)
    value = os.environ.get(name, "").strip()
    if not value:
        raise MissingEnvError(
            f"渠道 {provider} 缺少环境变量 {name}。请在项目根 .env 中填写该变量后重试。"
        )
    return value


def make_model_factory(*, provider: str, channel: dict, model: str, protocol: str,
                       timeout: float, max_tokens: int, model_settings: dict,
                       request_attempts: int = REQUEST_ATTEMPTS, signals: RunSignals | None = None) -> ModelFactory:
    """启动时读取密钥和写在 .env 里的渠道地址，缺了在创建日志前报错。"""
    base_url = channel["base_url"] if "base_url" in channel else _require_env(channel["base_url_env"], provider)
    return ModelFactory(protocol, model, base_url,
                        _require_env(channel["api_key_env"], provider), timeout, max_tokens,
                        request_attempts=request_attempts, model_settings=model_settings, signals=signals)
