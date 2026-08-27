"""LLM 调用封装：Gemini OpenAI 兼容端点，密钥走 .env，思考摘要单独返回。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
DEFAULT_MODEL = "gemini-flash-lite-latest"

ENV_PATH = Path(__file__).parent / ".env"


@dataclass
class ChatReply:
    content: str            # 模型输出正文（自由推理 + JSON 块），进对话线
    reasoning_content: str  # 思考摘要，仅落日志，不进对话线、不回传 API


class MissingApiKeyError(RuntimeError):
    pass


def _load_client(base_url: str) -> OpenAI:
    load_dotenv(ENV_PATH)
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise MissingApiKeyError(
            "未找到 GEMINI_API_KEY。请复制 .env.example 为 .env 并填入密钥后再运行 --ai 模式。"
        )
    return OpenAI(base_url=base_url, api_key=api_key)


def make_chat_fn(base_url: str = DEFAULT_BASE_URL, model: str = DEFAULT_MODEL, timeout: int = 60):
    """返回一个 chat_fn(messages) -> ChatReply，供 AIPlayer 注入。

    AIPlayer 只依赖 chat_fn 签名，测试时用假函数替代，零网络。
    """
    client = _load_client(base_url)

    def chat_fn(messages: list[dict]) -> ChatReply:
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            timeout=timeout,
            # 官方文档示例为双层嵌套：SDK 把外层 extra_body 并入请求体，
            # 服务端读取顶层的 extra_body.google.thinking_config
            extra_body={"extra_body": {"google": {"thinking_config": {"include_thoughts": True}}}},
        )
        message = response.choices[0].message
        return ChatReply(
            content=message.content or "",
            reasoning_content=getattr(message, "reasoning_content", None) or "",
        )

    return chat_fn
