"""AIPlayer：持有只增不改的对话线，通过注入的 chat_fn 与 LLM 交互。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable, Optional

import prompts
from models import INITIAL_COINS, MAX_BID, Observation
from players import Player

JSON_BLOCK_PATTERN = re.compile(r"```json\s*(.*?)```", re.DOTALL)
# 兜底用：提示词示例无围栏，模型常直接输出裸 JSON；出价 JSON 是扁平对象，不匹配嵌套花括号
BARE_JSON_PATTERN = re.compile(r"\{[^{}]*\}")
# 发言定界符：取代 JSON 字符串承载自由文本发言（实证模型内嵌未转义的 " 会毁掉 JSON）
SPEECH_OPEN_MARKER = "【公开发言】"
SPEECH_CLOSE_MARKER = "【发言结束】"
SPEECH_BLOCK_PATTERN = re.compile(r"【公开发言】\s*(.*?)\s*【发言结束】", re.DOTALL)

PARSE_FAILED = object()  # 解析失败的哨兵值，区别于任何合法出价


class CallFailedError(Exception):
    """连续两次调用失败（网络/余额等基础设施故障）：由轮级别收集后仅重跑本玩家。"""


class EmptyResponseError(Exception):
    """模型返回空正文（渠道故障的典型症状，如 responses 输出在 think 中被截断）：
    按调用失败处理——立即重试 1 次，仍空则升级 CallFailedError。"""


class ParseFailedError(CallFailedError):
    """原地重试 1 次后仍解析失败：升级为轮级失败（挂起等人工确认重跑），
    不再静默记 0 或把回复全文 fallback 公开。继承 CallFailedError 是为了
    直接复用 run_parallel_round 的轮级失败收集机制（controller 零改动）。"""


class InvalidBidError(CallFailedError):
    """解析成功但数值非法（超余额/超范围/非整数/超级出价与正常出价同值/
    超级出价机会已消耗），
    提醒重试 1 次后仍非法：升级为轮级失败挂起等人工，不再静默记 0。
    与 ParseFailedError 并列区分日志语义（格式问题 vs 规则违反）。"""


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
        # 规划环节（与下一局第 1 天出价合并）缓存的首日提交 (正常出价, 超级出价)，
        # decide_bid 第 1 天直接消费；缓存只保留到被消费，规划时先清后写
        self._pending_day1_bid: Optional[tuple[int, Optional[int]]] = None

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
        if entry.get("parse_failed"):
            if "result" in entry:
                # 旧场次日志的历史条目形态：当时静默记 0，新日志不再产生
                return "> 出价解析失败，按规则记 0（旧场次条目）\n\n---\n\n"
            return f"> {entry['phase']}：解析连续失败，已挂起等待人工重跑\n\n---\n\n"
        if "result" in entry:
            if entry.get("kind") == "speech":
                return f"> 公开发言内容：{entry['result']}\n\n---\n\n"
            # 旧场次日志的历史条目形态（fallback 机制已取消，新日志不再产生）
            if entry.get("kind") == "speech_fallback":
                return "> 发言块解析失败，重试回复全文已作为公开发言\n\n---\n\n"
            return f"> 出价结果：{entry['result']}\n\n---\n\n"
        return ""

    def _call(self, user_text: str, phase: str) -> str:
        """追加一条 user 消息并调用 LLM，assistant 正文进对话线。

        空正文抛 EmptyResponseError：空回基本是渠道/基础设施故障（如输出在
        think 块中被截断、剥离后为空），按调用失败走重试，不进解析路径。
        """
        self.messages.append({"role": "user", "content": user_text})
        content = self.chat_fn(self.messages)
        if not content.strip():
            raise EmptyResponseError("模型返回空正文")
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
    def _field_from_json_text(json_text: str, key: str, strict: bool):
        try:
            data = json.loads(json_text, strict=strict)
        except json.JSONDecodeError:
            return PARSE_FAILED
        if not isinstance(data, dict) or key not in data:
            return PARSE_FAILED
        return data[key]

    @staticmethod
    def parse_bid(content: str):
        """解析 bid 字段：与 parse_json_field(content, "bid") 等价，严格模式。"""
        return AIPlayer.parse_json_field(content, "bid")

    @staticmethod
    def parse_bid_object(content: str):
        """解析含 bid 键的 JSON 对象整体（可同时含可选的 super_bid 键）：
        优先取最后一个 ```json 围栏块；无围栏块时兜底取全文最后一个合法裸
        JSON 对象；对象缺 bid 键即 PARSE_FAILED。出价 JSON 是扁平对象。"""
        blocks = JSON_BLOCK_PATTERN.findall(content)
        if blocks:
            try:
                data = json.loads(blocks[-1])
            except json.JSONDecodeError:
                return PARSE_FAILED
            return data if isinstance(data, dict) and "bid" in data else PARSE_FAILED
        for candidate in reversed(BARE_JSON_PATTERN.findall(content)):
            try:
                data = json.loads(candidate)
            except json.JSONDecodeError:
                continue  # 不是合法 JSON，继续向前找
            if isinstance(data, dict) and "bid" in data:
                return data
            return PARSE_FAILED  # 最后一个合法 JSON 对象无 bid 键，不再向前找
        return PARSE_FAILED

    @staticmethod
    def validate_submission(
        bid: object, super_bid: object, coins: int, super_available: bool
    ) -> Optional[str]:
        """校验一次出价提交（正常出价 + 可选超级出价），返回违规原因（None = 合法）。

        校验条件与引擎 validate_bid / validate_super_bid 一致，另加次数检查；
        在 AIPlayer 内拦截是为了能给模型一次带原因的提醒重试，而不是静默记 0。
        """
        if type(bid) is not int:  # bool 是 int 子类，同样视为非法
            return f"bid 不是整数（{bid!r}）"
        if bid < 0 or bid > MAX_BID:
            return f"出价 {bid} 超出 0~{MAX_BID} 的范围"
        if bid > coins:
            return f"出价 {bid} 超过当前余额 {coins}"
        if super_bid is None:
            return None
        if type(super_bid) is not int:
            return f"super_bid 不是整数（{super_bid!r}）"
        if super_bid < 0 or super_bid > MAX_BID:
            return f"超级出价 {super_bid} 超出 0~{MAX_BID} 的范围"
        if super_bid == bid:
            return f"超级出价 {super_bid} 不能等于你的正常出价"
        if not super_available:
            return "超级出价本局机会已消耗且未命中恢复，不能再提交"
        return None

    def _interpret_bid_response(
        self, content: str, coins: int, super_available: bool
    ) -> tuple[Optional[tuple[int, Optional[int]]], Optional[str]]:
        """把一次回复解释为 (提交, None) 或 (None, 失败原因)。

        失败原因 "parse" 表示解析失败（缺少合法 JSON 出价块），
        其余字符串为具体的数值违规原因（用于提醒重试文案）。
        """
        data = self.parse_bid_object(content)
        if data is PARSE_FAILED:
            return None, "parse"
        super_bid = data.get("super_bid")
        reason = self.validate_submission(data["bid"], super_bid, coins, super_available)
        if reason is not None:
            return None, reason
        return (data["bid"], super_bid), None

    def _request_valid_bid(
        self,
        user_text: str,
        phase: str,
        context: str,
        coins: int,
        super_available: bool,
    ) -> tuple[tuple[int, Optional[int]], str]:
        """出价/规划共用的请求-校验闭环：一次请求 + 一次重试，返回 (提交, 正文)。

        解析失败与数值非法共用同一条重试路径，提醒文案各自不同
        （非法时写清具体违规原因）；第二次仍失败升级轮级失败挂起——
        解析失败抛 ParseFailedError，数值非法抛 InvalidBidError。
        """
        content = self._guarded_call(user_text, phase, context)
        submission, failure = self._interpret_bid_response(content, coins, super_available)
        if failure is not None:
            retry_text = (
                prompts.retry_bid_request()
                if failure == "parse"
                else prompts.invalid_bid_retry_request(failure)
            )
            retry_content = self._guarded_call(retry_text, f"{phase}_retry", context)
            submission, failure = self._interpret_bid_response(
                retry_content, coins, super_available
            )
            if failure is not None:
                self._write_log({"phase": phase, "parse_failed": True})
                if failure == "parse":
                    raise ParseFailedError(f"{self.name} 出价连续两次解析失败（{context}）")
                raise InvalidBidError(
                    f"{self.name} 出价连续两次非法（{context}）：{failure}"
                )
        result_text = json.dumps(
            {"bid": submission[0], "super_bid": submission[1]}, ensure_ascii=False
        )
        self._write_log({"phase": phase, "result": result_text})
        return submission, content

    def decide_bid(self, obs: Observation) -> tuple[int, Optional[int]]:
        """返回 (正常出价, 超级出价或 None)，保证数值合法（非法已走提醒重试）。

        第 1 天信息恒定，出价已在规划环节一并给出并校验过：直接消费缓存，
        不再发 LLM 调用。
        """
        if obs.day == 1 and self._pending_day1_bid is not None:
            submission = self._pending_day1_bid
            self._pending_day1_bid = None
            return submission

        context = f"第{len(obs.past_match_logs) + 1}局第{obs.day}天"
        submission, _ = self._request_valid_bid(
            prompts.bid_request(obs), "bid", context, obs.my_coins, obs.my_super_available
        )
        return submission

    # ---- 发言与规划 ----

    @staticmethod
    def parse_speech(content: str):
        """解析公开发言：三级路径——① 定界符提取（【公开发言】…【发言结束】，
        定界符之间是纯文本，引号与换行不受 JSON 转义约束）；② 缺结束标记时
        宽容取最后一个开始标记到回复末尾（发言按提示词必须放末尾）；
        ③ 兜底旧 JSON 格式（strict=False 宽容字面换行），仍无则 PARSE_FAILED。"""
        blocks = SPEECH_BLOCK_PATTERN.findall(content)
        if blocks and blocks[-1].strip():
            return blocks[-1].strip()
        open_index = content.rfind(SPEECH_OPEN_MARKER)
        if open_index != -1:
            tail = content[open_index + len(SPEECH_OPEN_MARKER):].strip()
            # 空块（开始标记后紧跟结束标记）：剥掉结束标记再看余量，空则继续兜底
            if tail.startswith(SPEECH_CLOSE_MARKER):
                tail = tail[len(SPEECH_CLOSE_MARKER):].strip()
            if tail:
                return tail
        return AIPlayer.parse_json_field(content, "speech", strict=False)

    def give_speech(self, user_text: str, phase: str, context: str = "") -> str:
        """公开发言：先规划发言策略，末尾【公开发言】…【发言结束】给出实际公开内容
        （旧 JSON 格式仍兼容，见 parse_speech）。

        返回实际公开的发言文本（其他玩家互看与 speeches.txt 只含此内容）；
        assistant 全文（含规划部分）照常进自己的对话线与日志，不做任何截断。
        解析失败重试 1 次，仍失败抛 ParseFailedError 升级为轮级失败
        （挂起等人工确认重跑）——不把无法解析的回复全文公开给其他玩家，
        垃圾全文会污染他们的对话线；原文已由 _call 完整记入日志，不丢内容。
        双调用失败抛 CallFailedError。
        """
        content = self._guarded_call(user_text, phase, context).strip()
        speech = self.parse_speech(content)
        if speech is PARSE_FAILED:
            retry_content = self._guarded_call(
                prompts.retry_speech_request(), f"{phase}_retry", context
            ).strip()
            speech = self.parse_speech(retry_content)
            if speech is PARSE_FAILED:
                self._write_log({"phase": phase, "parse_failed": True})
                raise ParseFailedError(f"{self.name} 发言连续两次解析失败（{phase}）")
        speech_text = speech if isinstance(speech, str) else json.dumps(speech, ensure_ascii=False)
        self._write_log({"phase": phase, "kind": "speech", "result": speech_text})
        return speech_text.strip()

    def plan_strategy(self, user_text: str, phase: str, context: str = "") -> str:
        """策略规划（与下一局第 1 天出价合并）：规划全文进对话线，只给自己看。

        回复末尾的 JSON 出价（可含 super_bid）缓存为下一局第 1 天提交
        （decide_bid 第 1 天直接消费，缓存只保留到被消费，开局重新规划时
        先清后写，避免暂停重跑残留旧值）；解析失败与数值非法同出价规则：
        提醒重试 1 次，仍失败抛 ParseFailedError / InvalidBidError 升级轮级失败。
        第 1 天余额恒为 INITIAL_COINS、超级出价恒可用，校验用常量。
        """
        self._pending_day1_bid = None
        submission, content = self._request_valid_bid(
            user_text, phase, context, INITIAL_COINS, super_available=True
        )
        self._pending_day1_bid = submission
        return content

    def final_summary(self, user_text: str, context: str = "") -> str:
        """终局观众总结：不回喂任何对话线（连自己的也不进），只落日志。
        空正文与调用异常同等处理（重试 1 次，仍失败抛 CallFailedError）。"""
        last_error: Optional[Exception] = None
        for attempt in (1, 2):
            try:
                content = self.chat_fn(self.messages + [{"role": "user", "content": user_text}])
                if not content.strip():
                    raise EmptyResponseError("模型返回空正文")
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
