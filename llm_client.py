"""LLM 调用封装：多 provider 分发（gemini/modal/hub/anthropic），密钥走 .env。

provider 与调用方式：
- gemini：Google 官方 OpenAI 兼容端点，chat completions（当前无玩家使用，代码保留）
- modal：Modal 托管的 Kimi-K3，OpenAI chat completions（K3 强制推理，不传 thinking 控制参数）
- hub：oaifree 中转，按玩家 protocol 字段二选一：responses API（默认）或 chat completions
- anthropic：oaifree 中转，Anthropic SDK（当前无玩家使用，代码保留）

chat_fn 统一签名为 (messages: list[dict]) -> str，只返回正文 content。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Callable

from dotenv import load_dotenv
from openai import OpenAI

ENV_PATH = Path(__file__).parent / ".env"

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
MODAL_BASE_URL = "https://modal-endpoint.invalid/v1"
HUB_BASE_URL = "https://hub.oaifree.com/v1"
HUB_ANTHROPIC_BASE_URL = "https://hub.oaifree.com/anthropic"

# 模型输出上限，数据来源：https://models.dev/api.json 官方 provider 条目（2026-08-28 拉取）
#   moonshotai | kimi-k3              out=131072
#   deepseek   | deepseek-v4-pro / deepseek-v4-flash  out=384000
#   google     | gemini-3.1-pro-preview / gemini-flash-latest  out=65536
#   openai     | gpt-5.6-sol          out=128000
#   anthropic  | claude-opus-4-6      out=128000
#   minimax    | MiniMax-M3           out=512000
#   zai        | glm-5.3              out=131072
MODEL_OUTPUT_LIMITS = {
    "moonshotai/Kimi-K3": 131072,
    "deepseek-v4-pro": 384000,
    "deepseek-v4-flash": 384000,
    "gemini-3.1-pro-preview": 65536,
    "gpt-5.6-sol": 128000,
    "claude-opus-4-6": 128000,
    "gemini-flash-latest": 65536,
    "MiniMax-M3": 512000,
    "glm-5.3": 131072,
}

# anthropic 的 max_tokens 是必填参数，未知模型的兜底值
ANTHROPIC_FALLBACK_MAX_TOKENS = 8192

ChatFn = Callable[[list[dict]], str]

_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)
_THINK_UNCLOSED_RE = re.compile(r"<think>.*$", re.DOTALL)


def strip_think_blocks(text: str) -> str:
    """剥离正文内嵌的 <think> 推理块（如 MiniMax-M3 的 responses 输出）。

    未闭合的 <think>（输出被截断时）连同其后内容一并去掉。
    """
    return _THINK_UNCLOSED_RE.sub("", _THINK_BLOCK_RE.sub("", text))


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


def _make_gemini_fn(model: str, timeout: int) -> ChatFn:
    api_key = _require_env("GEMINI_API_KEY", "gemini")
    client = OpenAI(base_url=GEMINI_BASE_URL, api_key=api_key)

    def chat_fn(messages: list[dict]) -> str:
        kwargs = {}
        if model in MODEL_OUTPUT_LIMITS:
            kwargs["max_tokens"] = MODEL_OUTPUT_LIMITS[model]
        response = client.chat.completions.create(
            model=model, messages=messages, timeout=timeout, **kwargs
        )
        return response.choices[0].message.content or ""

    return chat_fn


def _make_modal_fn(model: str, timeout: int) -> ChatFn:
    token_id = _require_env("MODAL_PROXY_TOKEN_ID", "modal")
    token_secret = _require_env("MODAL_PROXY_TOKEN_SECRET", "modal")
    client = OpenAI(base_url=MODAL_BASE_URL, api_key=f"{token_id}.{token_secret}")

    def chat_fn(messages: list[dict]) -> str:
        # Kimi K3 强制推理，传 reasoning_effort=none 会 400，省略 thinking 控制参数
        kwargs = {}
        if model in MODEL_OUTPUT_LIMITS:
            kwargs["max_tokens"] = MODEL_OUTPUT_LIMITS[model]
        response = client.chat.completions.create(
            model=model, messages=messages, timeout=timeout, **kwargs
        )
        return response.choices[0].message.content or ""

    return chat_fn


def _make_hub_fn(model: str, timeout: int, protocol: str = "responses") -> ChatFn:
    """oaifree 中转端点，protocol 二选一：

    - responses：OpenAI responses API，input 直接吃 messages 列表，取 output_text
    - chat：OpenAI chat completions（/v1/chat/completions），取 choices[0].message.content
    """
    if protocol not in ("responses", "chat"):
        raise ValueError(f"hub 未知 protocol：{protocol!r}（可选：responses、chat）")

    api_key = _require_env("HUB_API_KEY", "hub")
    client = OpenAI(base_url=HUB_BASE_URL, api_key=api_key)

    if protocol == "chat":

        def chat_fn(messages: list[dict]) -> str:
            kwargs = {}
            if model in MODEL_OUTPUT_LIMITS:
                kwargs["max_tokens"] = MODEL_OUTPUT_LIMITS[model]
            response = client.chat.completions.create(
                model=model, messages=messages, timeout=timeout, **kwargs
            )
            return response.choices[0].message.content or ""

        return chat_fn

    def responses_fn(messages: list[dict]) -> str:
        kwargs = {}
        if model in MODEL_OUTPUT_LIMITS:
            kwargs["max_output_tokens"] = MODEL_OUTPUT_LIMITS[model]
        response = client.responses.create(
            model=model, input=messages, timeout=timeout, **kwargs
        )
        return response.output_text or ""

    return responses_fn


def _make_anthropic_fn(model: str, timeout: int) -> ChatFn:
    """oaifree 中转的 Anthropic 端点：system 需从 messages 中单独提取。"""
    from anthropic import Anthropic  # 惰性导入，未用 claude 时无需安装

    api_key = _require_env("HUB_API_KEY", "anthropic")
    client = Anthropic(base_url=HUB_ANTHROPIC_BASE_URL, api_key=api_key)

    def chat_fn(messages: list[dict]) -> str:
        system_prompt = ""
        conversation = messages
        if messages and messages[0].get("role") == "system":
            system_prompt = messages[0]["content"]
            conversation = messages[1:]
        response = client.messages.create(
            model=model,
            max_tokens=MODEL_OUTPUT_LIMITS.get(model, ANTHROPIC_FALLBACK_MAX_TOKENS),
            system=system_prompt,
            messages=conversation,
            timeout=timeout,
        )
        return "".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        )

    return chat_fn


_PROVIDER_FACTORIES = {
    "gemini": _make_gemini_fn,
    "modal": _make_modal_fn,
    "hub": _make_hub_fn,
    "anthropic": _make_anthropic_fn,
}


def make_chat_fn(
    provider: str, model: str, timeout: int = 60, protocol: str | None = None
) -> ChatFn:
    """按 provider 构建 chat_fn(messages) -> str，供 AIPlayer 注入。

    protocol 仅对 hub 生效（responses/chat），缺省按 responses 处理。
    AIPlayer 只依赖 chat_fn 签名，测试时用假函数替代，零网络。
    """
    factory = _PROVIDER_FACTORIES.get(provider)
    if factory is None:
        known = "、".join(sorted(_PROVIDER_FACTORIES))
        raise ValueError(f"未知 provider：{provider!r}（可选：{known}）")
    if provider == "hub":
        raw_fn = _make_hub_fn(model, timeout, protocol or "responses")
    else:
        raw_fn = factory(model, timeout)

    def chat_fn(messages: list[dict]) -> str:
        return strip_think_blocks(raw_fn(messages))

    return chat_fn
