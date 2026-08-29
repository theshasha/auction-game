"""AIPlayer：持有只增不改的对话线，通过注入的 chat_fn 与 LLM 交互。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable, Optional

import prompts
from models import Observation
from players import Player

JSON_BLOCK_PATTERN = re.compile(r"```json\s*(.*?)```", re.DOTALL)
# 兜底用：提示词示例无围栏，模型常直接输出裸 JSON；出价 JSON 是扁平对象，不匹配嵌套花括号
BARE_JSON_PATTERN = re.compile(r"\{[^{}]*\}")

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
        persona_title: str,
        persona_text: str,
        predictions: list[tuple[str, int]],
        all_names: list[str],
        chat_fn: Callable[[list[dict]], str],
        log_path: Optional[Path] = None,
    ):
        super().__init__(name)
        self.messages: list[dict] = [
            {
                "role": "system",
                "content": prompts.build_system_prompt(
                    name, persona_title, persona_text, predictions, all_names
                ),
            }
        ]
        self.chat_fn = chat_fn
        self.log_path = log_path
        # 规划环节（与下一局第 1 天出价合并）缓存的首日出价，decide_bid 第 1 天直接消费
        self._pending_day1_bid: Optional[int] = None

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
        """按 jsonl 条目的三种形态渲染 markdown 片段。"""
        if "user" in entry and "content" in entry:
            return (
                f"## {entry['phase']}\n\n"
                f"**【user】**\n\n{entry['user']}\n\n"
                f"**【assistant】**\n\n{entry['content']}\n\n---\n\n"
            )
        if "error" in entry:
            context = entry.get("context")
            where = f"{entry['phase']}，{context}" if context else entry["phase"]
            return f"> 调用失败（{where}，第 {entry['attempt']} 次）：{entry['error']}\n\n"
        if "result" in entry:
            if entry.get("kind") == "speech":
                return f"> 公开发言内容：{entry['result']}\n\n---\n\n"
            if entry.get("kind") == "speech_fallback":
                return "> 发言 JSON 解析失败，重试回复全文已作为公开发言\n\n---\n\n"
            if entry.get("parse_failed"):
                return "> 出价解析失败，按规则记 0\n\n---\n\n"
            return f"> 出价结果：{entry['result']}\n\n---\n\n"
        return ""

    def _call(self, user_text: str, phase: str) -> str:
        """追加一条 user 消息并调用 LLM，assistant 正文进对话线。"""
        self.messages.append({"role": "user", "content": user_text})
        content = self.chat_fn(self.messages)
        self.messages.append({"role": "assistant", "content": content})
        self._write_log({"phase": phase, "user": user_text, "content": content})
        return content

    def _guarded_call(self, user_text: str, phase: str, context: str = "") -> str:
        """调用 LLM，失败立即重试 1 次（重试前回滚未应答的 user 消息）。

        context: 轮次描述（如「第3局第2天」），仅用于失败日志定位。
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
                entry = {"phase": phase, "attempt": attempt, "error": f"{type(exc).__name__}: {exc}"}
                if context:
                    entry["context"] = context
                self._write_log(entry)
        raise CallFailedError(
            f"{self.name} 连续两次调用失败（{phase}）：{type(last_error).__name__}: {last_error}"
        )

    # ---- 出价 ----

    @staticmethod
    def parse_json_field(content: str, key: str, strict: bool = True):
        """解析指定键的 JSON 字段：优先取最后一个 ```json 围栏块；无围栏块时兜底取
        全文最后一个合法裸 JSON 对象（提示词示例即无围栏）再查 key，仍无则返回
        PARSE_FAILED（无 key 即失败，不再向前找）。
        strict=False 宽容 JSON 字符串内的字面换行（发言常为未转义的多段文本）。"""
        blocks = JSON_BLOCK_PATTERN.findall(content)
        if blocks:
            return AIPlayer._field_from_json_text(blocks[-1], key, strict)
        for candidate in reversed(BARE_JSON_PATTERN.findall(content)):
            try:
                data = json.loads(candidate, strict=strict)
            except json.JSONDecodeError:
                continue  # 不是合法 JSON，继续向前找
            if isinstance(data, dict) and key in data:
                return data[key]
            return PARSE_FAILED  # 最后一个合法 JSON 对象无目标键，不再向前找
        return PARSE_FAILED

    @staticmethod
    def parse_bid(content: str):
        """解析 bid 字段：与 parse_json_field(content, "bid") 等价，严格模式。"""
        return AIPlayer.parse_json_field(content, "bid")

    @staticmethod
    def _field_from_json_text(json_text: str, key: str, strict: bool):
        try:
            data = json.loads(json_text, strict=strict)
        except json.JSONDecodeError:
            return PARSE_FAILED
        if not isinstance(data, dict) or key not in data:
            return PARSE_FAILED
        return data[key]

    def decide_bid(self, obs: Observation) -> int:
        # 第 1 天信息恒定，出价已在规划环节一并给出：直接消费缓存，不再发 LLM 调用
        if obs.day == 1 and self._pending_day1_bid is not None:
            bid = self._pending_day1_bid
            self._pending_day1_bid = None
            return bid

        context = f"第{len(obs.past_match_logs) + 1}局第{obs.day}天"
        content = self._guarded_call(prompts.bid_request(obs), "bid", context)

        bid = self.parse_bid(content)
        if bid is PARSE_FAILED:
            # 模型输出格式问题（非基础设施故障）：按规则重试 1 次，仍失败记 0
            retry_content = self._guarded_call(
                prompts.retry_bid_request(), "bid_retry", context
            )
            bid = self.parse_bid(retry_content)
            if bid is PARSE_FAILED:
                self._write_log({"phase": "bid", "parse_failed": True, "result": 0})
                return 0

        # 数值是否合法由引擎统一校验（非法记 0 并标 illegal），这里原样返回不重试
        self._write_log({"phase": "bid", "result": bid})
        return bid

    # ---- 发言与规划 ----

    def give_speech(self, user_text: str, phase: str, context: str = "") -> str:
        """公开发言：先规划发言策略，末尾 JSON {"speech": ...} 给出实际公开内容。

        返回实际公开的发言文本（其他玩家互看与 speeches.txt 只含此内容）；
        assistant 全文（含规划部分）照常进自己的对话线与日志，不做任何截断。
        speech 解析宽容字面换行（strict=False）。解析失败重试 1 次，仍失败
        则把重试回复全文当作公开发言（不丢内容）。双调用失败抛 CallFailedError。
        """
        content = self._guarded_call(user_text, phase, context).strip()
        speech = self.parse_json_field(content, "speech", strict=False)
        if speech is PARSE_FAILED:
            retry_content = self._guarded_call(
                prompts.retry_speech_request(), f"{phase}_retry", context
            ).strip()
            speech = self.parse_json_field(retry_content, "speech", strict=False)
            if speech is PARSE_FAILED:
                self._write_log(
                    {"phase": phase, "kind": "speech_fallback", "result": retry_content}
                )
                return retry_content
        speech_text = speech if isinstance(speech, str) else json.dumps(speech, ensure_ascii=False)
        self._write_log({"phase": phase, "kind": "speech", "result": speech_text})
        return speech_text.strip()

    def plan_strategy(self, user_text: str, phase: str, context: str = "") -> str:
        """策略规划（与下一局第 1 天出价合并）：规划全文进对话线，只给自己看。

        回复末尾的 JSON 出价缓存为下一局第 1 天出价（decide_bid 第 1 天直接消费，
        缓存只保留到被消费，开局重新规划时先清后写，避免暂停重跑残留旧值）；
        解析失败与出价同规则：重试 1 次，仍失败记 0。
        """
        self._pending_day1_bid = None
        content = self._guarded_call(user_text, phase, context).strip()
        bid = self.parse_bid(content)
        if bid is PARSE_FAILED:
            retry_content = self._guarded_call(
                prompts.retry_bid_request(), f"{phase}_retry", context
            )
            bid = self.parse_bid(retry_content)
            if bid is PARSE_FAILED:
                self._write_log({"phase": phase, "parse_failed": True, "result": 0})
                self._pending_day1_bid = 0
                return content
        self._write_log({"phase": phase, "result": bid})
        self._pending_day1_bid = bid
        return content

    def final_summary(self, user_text: str, context: str = "") -> str:
        """终局观众总结：不回喂任何对话线（连自己的也不进），只落日志。"""
        last_error: Optional[Exception] = None
        for attempt in (1, 2):
            try:
                content = self.chat_fn(self.messages + [{"role": "user", "content": user_text}])
                break
            except Exception as exc:
                last_error = exc
                entry = {
                    "phase": "final_summary",
                    "attempt": attempt,
                    "error": f"{type(exc).__name__}: {exc}",
                }
                if context:
                    entry["context"] = context
                self._write_log(entry)
        else:
            raise CallFailedError(
                f"{self.name} 连续两次调用失败（final_summary）："
                f"{type(last_error).__name__}: {last_error}"
            )
        self._write_log({"phase": "final_summary", "user": user_text, "content": content})
        return content.strip()
