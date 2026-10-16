"""三协议模型工厂：完整流检查及每请求至多一次基础设施重试。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import os
from pathlib import Path

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

ENV_PATH = Path(__file__).parent / ".env"

class IncompleteStream(RuntimeError):
    """输出截断或缺少明确成功状态；不交付部分结果。"""


def retryable_request_error(exc: Exception) -> bool:
    """按 SDK 异常类型及状态码判断；配置错误与截断不重试。"""
    if isinstance(exc, (ModelHTTPError, openai.APIStatusError, anthropic.APIStatusError)):
        return exc.status_code in (408, 429) or 500 <= exc.status_code <= 599
    if isinstance(exc, ModelAPIError) and exc.__cause__ is not None:
        return retryable_request_error(exc.__cause__)
    return isinstance(exc, (openai.APIConnectionError, anthropic.APIConnectionError,
                            http.NetworkError, http.TimeoutException, http.RemoteProtocolError))


class BufferedRequest(WrapperModel):
    """完整消费后才交付，失败流隔离；每个请求至多重发一次。

    Anthropic 公开响应可观测 stop_reason，无法单独识别缺失的原始 message_stop。
    这里沿用已验证的明确成功 finish_reason 边界，不另写 SSE 框架。
    """

    def __init__(self, wrapped):
        super().__init__(wrapped)
        self.request_errors: list[dict] = []
        self._request_number = 0  # 包装器每任务创建；网络重发共用同一请求序号

    @asynccontextmanager
    async def request_stream(self, messages, model_settings, model_request_parameters, run_context=None):
        self._request_number += 1
        request_number = self._request_number
        for attempt in (1, 2):
            try:
                async with self.wrapped.request_stream(
                    messages, model_settings, model_request_parameters, run_context
                ) as stream:
                    async for _ in stream:
                        pass
                    response = stream.get()
                if response.finish_reason not in ("stop", "tool_call"):
                    raise IncompleteStream(f"finish_reason={response.finish_reason!r}")
                break
            except Exception as exc:
                retryable = retryable_request_error(exc)
                self.request_errors.append({"request": request_number, "attempt": attempt, "type": type(exc).__name__,
                                            "message": str(exc), "retryable": retryable})
                if attempt == 2 or not retryable:
                    raise
        # yield 在重试范围外：Agent 校验失败不会重新执行已完成的请求。
        yield CompletedStreamedResponse(response, model_request_parameters=model_request_parameters)


@dataclass(frozen=True)
class ModelFactory:
    """只保存配置；在调用所在的循环内创建及关闭 SDK/HTTP 客户端。

    transport 仅供离线注入。任务在自己的事件循环中使用 open() 及 settings。
    配置的校验入口是 configuration.load_configuration。
    """

    protocol: str
    model: str
    base_url: str
    api_key: str = field(repr=False)
    timeout: float
    max_tokens: int
    transport: http.AsyncBaseTransport | None = None

    @property
    def settings(self) -> dict:
        return {"timeout": self.timeout, "max_tokens": self.max_tokens}

    @asynccontextmanager
    async def open(self):
        async with http.AsyncClient(transport=self.transport) as client:
            if self.protocol == "anthropic":
                sdk = AsyncAnthropic(base_url=self.base_url, api_key=self.api_key,
                                     http_client=client, max_retries=0)
                model = AnthropicModel(self.model, provider=AnthropicProvider(anthropic_client=sdk))
            elif self.protocol in ("chat", "responses"):
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
            else:
                raise ValueError(f"未知 protocol：{self.protocol!r}")
            # SDK 使用注入的 HTTP 客户端；由外层唯一所有者关闭（也覆盖构造异常）。
            yield BufferedRequest(model)


class MissingApiKeyError(RuntimeError):
    pass


def _require_env(name: str, provider: str) -> str:
    load_dotenv(ENV_PATH)
    value = os.environ.get(name, "").strip()
    if not value:
        raise MissingApiKeyError(
            f"provider={provider} 未找到 {name}。请在 .env 中填入后再运行 --ai 模式。"
        )
    return value


def make_model_factory(*, provider: str, channel: dict, model: str, protocol: str,
                       timeout: float, max_tokens: int) -> ModelFactory:
    """启动时解析密钥，确保缺密钥在创建比赛日志前暴露。"""
    return ModelFactory(protocol, model, channel["base_url"],
                        _require_env(channel["api_key_env"], provider), timeout, max_tokens)
