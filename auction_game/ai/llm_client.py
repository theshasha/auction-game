"""三协议模型工厂：完整流检查及每请求至多一次基础设施重试。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import os

from dotenv import load_dotenv
import httpx2 as http
from anthropic import AsyncAnthropic
import anthropic
from openai import AsyncOpenAI
import openai
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError
from pydantic_ai.models import CompletedStreamedResponse
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.openai import OpenAIProvider
from auction_game.paths import ENV_PATH


class IncompleteStream(RuntimeError):
    """输出截断或缺少成功结束标记。"""


def retryable_request_error(exc: Exception) -> bool:
    """超时、限流、5xx 和网络错误可以重试。"""
    if isinstance(exc, ModelHTTPError):
        return exc.status_code in (408, 429) or 500 <= exc.status_code <= 599
    if isinstance(exc, ModelAPIError) and exc.__cause__ is not None:
        return retryable_request_error(exc.__cause__)
    return isinstance(exc, (openai.APIConnectionError, anthropic.APIConnectionError,
                            http.NetworkError, http.TimeoutException, http.RemoteProtocolError))


class BufferedRequest(WrapperModel):
    """完整消费流并确认正常结束后才交付；每个请求至多重发一次。

    Anthropic 公开响应只提供 stop_reason，原始 message_stop 缺失时无法单独识别。
    """

    def __init__(self, wrapped, *, attempts=2):
        super().__init__(wrapped)
        self.request_errors: list[dict] = []
        self.attempts = attempts
        self._request_number = 0  # 每个任务新建包装器；网络重发共用同一请求序号

    @asynccontextmanager
    async def request_stream(self, messages, model_settings, model_request_parameters, run_context=None):
        self._request_number += 1
        request_number = self._request_number
        for attempt in range(1, self.attempts + 1):
            try:
                async with self.wrapped.request_stream(
                    messages, model_settings, model_request_parameters, run_context
                ) as stream:
                    async for _ in stream:
                        pass
                    response = stream.get()
                if response.finish_reason != "stop":
                    raise IncompleteStream(f"finish_reason={response.finish_reason!r}")
                break
            except Exception as exc:
                retryable = retryable_request_error(exc)
                self.request_errors.append({"request": request_number, "attempt": attempt, "type": type(exc).__name__,
                                            "message": str(exc), "retryable": retryable})
                if attempt == self.attempts or not retryable:
                    raise
        # yield 在重试范围外：Agent 要求纠错时发新请求，不重发已完成的请求。
        yield CompletedStreamedResponse(response, model_request_parameters=model_request_parameters)


@dataclass(frozen=True)
class ModelFactory:
    """保存模型配置；open() 在调用方的事件循环里创建并关闭 SDK 和 HTTP 客户端。

    transport 用于测试和离线演示注入假传输。
    """

    protocol: str
    model: str
    base_url: str
    api_key: str = field(repr=False)
    timeout: float
    max_tokens: int
    transport: http.AsyncBaseTransport | None = None
    request_attempts: int = 2  # 连通检查只请求一次；比赛每个请求最多尝试两次。
    model_settings: dict = field(default_factory=dict)

    @property
    def settings(self) -> dict:
        return {**self.model_settings, "timeout": self.timeout, "max_tokens": self.max_tokens}

    @asynccontextmanager
    async def open(self):
        async with http.AsyncClient(transport=self.transport) as client:
            if self.protocol == "anthropic":
                sdk = AsyncAnthropic(base_url=self.base_url, api_key=self.api_key,
                                     http_client=client, max_retries=0)
                model = AnthropicModel(self.model, provider=AnthropicProvider(anthropic_client=sdk))
            else:
                sdk = AsyncOpenAI(base_url=self.base_url, api_key=self.api_key,
                                  http_client=client, max_retries=0)
                provider = OpenAIProvider(openai_client=sdk)
                if self.protocol == "chat":
                    model = OpenAIChatModel(self.model, provider=provider, profile={
                        "openai_chat_streaming_requires_finish_reason": True,
                        "openai_chat_supports_max_completion_tokens": False,
                    })
                else:
                    model = OpenAIResponsesModel(self.model, provider=provider)
            # SDK 使用注入的 HTTP 客户端，由外层 async with 统一关闭，构造失败时也会关闭。
            yield BufferedRequest(model, attempts=self.request_attempts)


class MissingApiKeyError(RuntimeError):
    pass


def _require_env(name: str, provider: str) -> str:
    load_dotenv(ENV_PATH)
    value = os.environ.get(name, "").strip()
    if not value:
        raise MissingApiKeyError(
            f"渠道 {provider} 缺少密钥 {name}。请在项目根 .env 中填写该变量后重试。"
        )
    return value


def make_model_factory(*, provider: str, channel: dict, model: str, protocol: str,
                       timeout: float, max_tokens: int, model_settings: dict,
                       request_attempts: int = 2) -> ModelFactory:
    """启动时读取密钥，缺密钥在创建日志前报错。"""
    return ModelFactory(protocol, model, channel["base_url"],
                        _require_env(channel["api_key_env"], provider), timeout, max_tokens,
                        request_attempts=request_attempts, model_settings=model_settings)
