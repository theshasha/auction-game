"""Pydantic AI 三协议模型工厂及过渡文本适配器。

完整消费流并验证成功结束才交付正文。SDK 与模型层均不重试；过渡期仍由
AIPlayer 控制失败重试。每次同步调用创建自己的事件循环及客户端。
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import os
import re
from pathlib import Path
from typing import Callable

from dotenv import load_dotenv
import httpx2 as http
from anthropic import AsyncAnthropic
from openai import AsyncOpenAI
from pydantic_ai.messages import (
    ModelRequest, ModelResponse, SystemPromptPart, TextPart, UserPromptPart,
)
from pydantic_ai.models import CompletedStreamedResponse, ModelRequestParameters
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.openai import OpenAIProvider

ENV_PATH = Path(__file__).parent / ".env"

ChatFn = Callable[[list[dict]], str]

_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)
_THINK_UNCLOSED_RE = re.compile(r"<think>.*$", re.DOTALL)


def strip_think_blocks(text: str) -> str:
    """剥离正文内嵌的 <think> 推理块（如 MiniMax-M3 的 responses 输出）。

    未闭合的 <think>（输出被截断时）连同其后内容一并去掉。
    """
    return _THINK_UNCLOSED_RE.sub("", _THINK_BLOCK_RE.sub("", text))


class IncompleteStream(RuntimeError):
    """输出截断或缺少明确成功状态；不交付部分结果。"""


class BufferedRequest(WrapperModel):
    """消费完整 SDK 流，检查结束状态；不做网络重试。

    Anthropic 公开响应可观测 stop_reason，无法单独识别缺失的原始 message_stop。
    这里沿用已验证的明确成功 finish_reason 边界，不另写 SSE 框架。
    """

    @asynccontextmanager
    async def request_stream(self, messages, model_settings, model_request_parameters, run_context=None):
        async with self.wrapped.request_stream(
            messages, model_settings, model_request_parameters, run_context
        ) as stream:
            async for _ in stream:
                pass
            response = stream.get()
        if response.finish_reason not in ("stop", "tool_call"):
            raise IncompleteStream(f"finish_reason={response.finish_reason!r}")
        yield CompletedStreamedResponse(response, model_request_parameters=model_request_parameters)


@dataclass(frozen=True)
class ModelFactory:
    """只保存配置；在调用所在的循环内创建及关闭 SDK/HTTP 客户端。

    transport 仅供离线注入。第三步 Agent 可直接复用 open() 及 settings。
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


def _text_messages(messages: list[dict]) -> list:
    """无状态转换旧 AIPlayer 历史，保持顺序，不额外追加当前提示。"""
    converted = []
    for message in messages:
        role, content = message["role"], message["content"]
        if role == "assistant":
            converted.append(ModelResponse(parts=[TextPart(content)]))
        elif role in ("system", "user"):
            part = SystemPromptPart(content) if role == "system" else UserPromptPart(content)
            converted.append(ModelRequest(parts=[part]))
        else:
            raise ValueError(f"文本适配器不支持 role={role!r}")
    return converted


def make_chat_fn(*, provider: str, channel: dict, model: str, protocol: str,
                 timeout: float, max_tokens: int) -> ChatFn:
    """薄文本适配器；第三步迁移 Agent 后移除，不持有任何对话状态。"""
    return text_chat_fn(make_model_factory(provider=provider, channel=channel, model=model,
                                         protocol=protocol, timeout=timeout, max_tokens=max_tokens))


def text_chat_fn(factory: ModelFactory) -> ChatFn:
    """同步线程边界；工厂可在离线测试中注入 MockTransport。"""
    async def call(messages):
        async with factory.open() as model:
            async with model.request_stream(
                _text_messages(messages), factory.settings, ModelRequestParameters()
            ) as stream:
                async for _ in stream:
                    pass
                response = stream.get()
        return strip_think_blocks("".join(p.content for p in response.parts if isinstance(p, TextPart)))

    def chat_fn(messages: list[dict]) -> str:
        return asyncio.run(call(messages))

    return chat_fn
