"""AIPlayer：持有只增不改的对话线，通过注入的 chat_fn 与 LLM 交互。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable, Optional

import prompts
from models import Observation
from players import Player

SPEECH_LIMIT = 500

JSON_BLOCK_PATTERN = re.compile(r"```json\s*(.*?)```", re.DOTALL)

PARSE_FAILED = object()  # 解析失败的哨兵值，区别于任何合法出价


class CallFailedError(Exception):
    """连续两次调用失败（网络/余额等基础设施故障）：由轮级别收集后仅重跑本玩家。"""


class AIPlayer(Player):
    """LLM 驱动的玩家。

    前缀保护：messages 初始化后只 append，唯一的截断是 rollback 到 checkpoint
    （暂停/失败回滚，只动尾部）。chat_fn 可注入，测试零网络。
    连续两次调用失败抛 CallFailedError，由 run_parallel_round 收集失败名单、
    仅重跑失败的玩家；对话线在抛出前已回滚到本轮 checkpoint 状态。
    """

    def __init__(
        self,
        name: str,
        persona: str,
        all_names: list[str],
        chat_fn: Callable[[list[dict]], str],
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
        self._append_markdown(entry)

    def _append_markdown(self, entry: dict) -> None:
        """把本条 jsonl 日志渲染追加到同名 .md，供 VSCode 实时分玩家阅读。

        jsonl 是结构化源头，md 只是视图；正文不套代码围栏，
        assistant 自带的 ```json 块原样写入即可正常渲染。
        """
        md_path = self.log_path.with_suffix(".md")
        is_new_file = not md_path.exists()
        with md_path.open("a", encoding="utf-8") as file:
            if is_new_file:
                file.write(f"# {self.name} 对话记录\n\n")
            file.write(self._render_markdown_entry(entry))

    @staticmethod
    def _render_markdown_entry(entry: dict) -> str:
        """按 jsonl 条目的四种形态渲染 markdown 片段。"""
        if "user" in entry and "content" in entry:
            return (
                f"## {entry['phase']}\n\n"
                f"**【user】**\n\n{entry['user']}\n\n"
                f"**【assistant】**\n\n{entry['content']}\n\n---\n\n"
            )
        if "error" in entry:
            return f"> 调用失败（{entry['phase']}，第 {entry['attempt']} 次）：{entry['error']}\n\n"
        if "result" in entry:
            if entry.get("parse_failed"):
                return "> 出价解析失败，按规则记 0\n\n---\n\n"
            return f"> 出价结果：{entry['result']}\n\n---\n\n"
        if entry.get("truncated"):
            return f"> 发言截断：原文 {entry['original_length']} 字，已截断至 {SPEECH_LIMIT} 字\n\n"
        return ""

    def _call(self, user_text: str, phase: str) -> str:
        """追加一条 user 消息并调用 LLM，assistant 正文进对话线。"""
        self.messages.append({"role": "user", "content": user_text})
        content = self.chat_fn(self.messages)
        self.messages.append({"role": "assistant", "content": content})
        self._write_log({"phase": phase, "user": user_text, "content": content})
        return content

    def _guarded_call(self, user_text: str, phase: str) -> str:
        """调用 LLM，失败立即重试 1 次（重试前回滚未应答的 user 消息）。

        连续两次失败：抛 CallFailedError，对话线保持在本轮 checkpoint 状态，
        由轮级别收集失败名单后仅重跑本玩家。
        """
        last_error: Optional[Exception] = None
        for attempt in (1, 2):
            mark = len(self.messages)
            try:
                return self._call(user_text, phase)
            except Exception as exc:
                last_error = exc
                self.rollback(mark)  # 丢弃未应答的 user 消息，保持对话线完整
                self._write_log(
                    {"phase": phase, "attempt": attempt, "error": f"{type(exc).__name__}: {exc}"}
                )
        raise CallFailedError(
            f"{self.name} 连续两次调用失败（{phase}）：{type(last_error).__name__}: {last_error}"
        )

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
        content = self._guarded_call(prompts.bid_request(obs), "bid")

        bid = self.parse_bid(content)
        if bid is PARSE_FAILED:
            # 模型输出格式问题（非基础设施故障）：按规则重试 1 次，仍失败记 0
            retry_content = self._guarded_call(prompts.retry_bid_request(), "bid_retry")
            bid = self.parse_bid(retry_content)
            if bid is PARSE_FAILED:
                self._write_log({"phase": "bid", "parse_failed": True, "result": 0})
                return 0

        # 数值是否合法由引擎统一校验（非法记 0 并标 illegal），这里原样返回不重试
        self._write_log({"phase": "bid", "result": bid})
        return bid

    # ---- 发言与规划 ----

    def give_speech(self, user_text: str, phase: str) -> str:
        """发言：正文截断 500 字，完整进对话线（公开信息）。双失败抛 CallFailedError。"""
        content = self._guarded_call(user_text, phase)
        speech = content.strip()
        if len(speech) > SPEECH_LIMIT:
            self._write_log({"phase": phase, "truncated": True, "original_length": len(speech)})
            speech = speech[:SPEECH_LIMIT]
        return speech

    def plan_strategy(self, user_text: str, phase: str) -> str:
        """策略规划：与发言同机制，只给自己看（不进任何公开渠道）。"""
        return self.give_speech(user_text, phase)

    def final_summary(self, user_text: str) -> str:
        """终局观众总结：不回喂任何对话线（连自己的也不进），只落日志。"""
        last_error: Optional[Exception] = None
        for attempt in (1, 2):
            try:
                content = self.chat_fn(self.messages + [{"role": "user", "content": user_text}])
                break
            except Exception as exc:
                last_error = exc
                self._write_log(
                    {"phase": "final_summary", "attempt": attempt, "error": f"{type(exc).__name__}: {exc}"}
                )
        else:
            raise CallFailedError(
                f"{self.name} 连续两次调用失败（final_summary）："
                f"{type(last_error).__name__}: {last_error}"
            )
        self._write_log({"phase": "final_summary", "user": user_text, "content": content})
        return content.strip()[:SPEECH_LIMIT]
