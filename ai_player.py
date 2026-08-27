"""AIPlayer：持有只增不改的对话线，通过注入的 chat_fn 与 LLM 交互。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable, Optional

import prompts
from llm_client import ChatReply
from models import Observation
from players import Player

SPEECH_LIMIT = 500

JSON_BLOCK_PATTERN = re.compile(r"```json\s*(.*?)```", re.DOTALL)

PARSE_FAILED = object()  # 解析失败的哨兵值，区别于任何合法出价


class AIPlayer(Player):
    """LLM 驱动的玩家。

    前缀保护：messages 初始化后只 append，唯一的截断是 rollback 到 checkpoint
    （暂停回滚，只动尾部）。chat_fn 可注入，测试零网络。
    """

    def __init__(
        self,
        name: str,
        persona: str,
        all_names: list[str],
        chat_fn: Callable[[list[dict]], ChatReply],
        log_path: Optional[Path] = None,
    ):
        super().__init__(name)
        self.messages: list[dict] = [
            {"role": "system", "content": prompts.build_system_prompt(name, persona, all_names)}
        ]
        self.chat_fn = chat_fn
        self.log_path = log_path

    # ---- 对话线维护 ----

    def checkpoint(self) -> int:
        """记录当前对话线长度，供暂停回滚使用。"""
        return len(self.messages)

    def rollback(self, checkpoint: int) -> None:
        """截断到 checkpoint，只动尾部，前缀逐字节不变。"""
        del self.messages[checkpoint:]

    def _write_log(self, entry: dict) -> None:
        if self.log_path is None:
            return
        self.log_path.parent.mkdir(exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _call(self, user_text: str, phase: str) -> ChatReply:
        """追加一条 user 消息并调用 LLM，assistant 正文进对话线，思考内容只落日志。"""
        self.messages.append({"role": "user", "content": user_text})
        reply = self.chat_fn(self.messages)
        self.messages.append({"role": "assistant", "content": reply.content})
        self._write_log(
            {
                "phase": phase,
                "user": user_text,
                "content": reply.content,
                "reasoning_content": reply.reasoning_content,
            }
        )
        return reply

    # ---- 出价 ----

    @staticmethod
    def parse_bid(content: str):
        """取正文中最后一个 ```json 块并解析 bid 字段；失败返回 PARSE_FAILED。"""
        blocks = JSON_BLOCK_PATTERN.findall(content)
        if not blocks:
            return PARSE_FAILED
        try:
            data = json.loads(blocks[-1])
        except json.JSONDecodeError:
            return PARSE_FAILED
        if not isinstance(data, dict) or "bid" not in data:
            return PARSE_FAILED
        return data["bid"]

    def decide_bid(self, obs: Observation) -> int:
        try:
            reply = self._call(prompts.bid_request(obs), "bid")
        except Exception as exc:  # 超时/API 异常：按规则视为出价 0
            self._write_log({"phase": "bid", "error": f"{type(exc).__name__}: {exc}", "result": 0})
            return 0

        bid = self.parse_bid(reply.content)
        if bid is PARSE_FAILED:
            try:  # 重试 1 次：明确指出问题，让模型修正
                retry_reply = self._call(prompts.retry_bid_request(), "bid_retry")
                bid = self.parse_bid(retry_reply.content)
            except Exception as exc:
                self._write_log({"phase": "bid_retry", "error": f"{type(exc).__name__}: {exc}", "result": 0})
                return 0
            if bid is PARSE_FAILED:
                self._write_log({"phase": "bid", "parse_failed": True, "result": 0})
                return 0

        # 数值是否合法由引擎统一校验（非法记 0 并标 illegal），这里原样返回不重试
        self._write_log({"phase": "bid", "result": bid})
        return bid

    # ---- 发言与规划 ----

    def give_speech(self, user_text: str, phase: str) -> Optional[str]:
        """发言：正文截断 500 字，完整进对话线（公开信息）。异常视为放弃发言。"""
        try:
            reply = self._call(user_text, phase)
        except Exception as exc:
            self._write_log({"phase": phase, "error": f"{type(exc).__name__}: {exc}", "absent": True})
            return None
        speech = reply.content.strip()
        truncated = len(speech) > SPEECH_LIMIT
        if truncated:
            self._write_log({"phase": phase, "truncated": True, "original_length": len(speech)})
            speech = speech[:SPEECH_LIMIT]
        return speech

    def plan_strategy(self, user_text: str, phase: str) -> Optional[str]:
        """策略规划：与发言同机制，只给自己看（不进任何公开渠道）。"""
        return self.give_speech(user_text, phase)

    def final_summary(self, user_text: str) -> Optional[str]:
        """终局观众总结：不回喂任何对话线（连自己的也不进），只落日志。"""
        try:
            reply = self.chat_fn(self.messages + [{"role": "user", "content": user_text}])
        except Exception as exc:
            self._write_log({"phase": "final_summary", "error": f"{type(exc).__name__}: {exc}", "absent": True})
            return None
        self._write_log(
            {
                "phase": "final_summary",
                "user": user_text,
                "content": reply.content,
                "reasoning_content": reply.reasoning_content,
            }
        )
        return reply.content.strip()[:SPEECH_LIMIT]
