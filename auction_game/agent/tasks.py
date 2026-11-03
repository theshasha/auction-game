"""个人赛任务：解析文本输出、校验出价，格式错误和违规出价共用一次纠错。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic_ai import Agent, ModelRetry, RunContext, TextOutput
from pydantic_ai.messages import ModelMessage
from pydantic_ai.usage import UsageLimits

from auction_game.llm.execution import AgentExecutor, ExecutionResult
from auction_game.game.settings import GameSettings, validate_submission
from auction_game.llm.client import ModelFactory
from auction_game.agent.text_output import (BRIEF_BID_FORMAT, LetterNames, OutputFormatError, output_requirement,
                                            parse_text_output)

Task = Literal["bid", "plan", "speech", "memory", "summary"]


@dataclass
class AuctionDeps:
    game: GameSettings
    coins: int


def render_task_result(result: dict | str) -> list[str]:
    """把任务结果渲染成私人日志里的小节。"""
    if not isinstance(result, dict):
        return [f"### 总结\n\n{result}"]
    lines = []
    if "strategy" in result:
        lines.append(f"### 私有策略\n\n{result['strategy']}")
    if "memory" in result:
        lines.append(f"### 累计私有记忆\n\n{result['memory']}")
    if "speech" in result:
        lines.append(f"### 公开发言\n\n{result['speech']}")
    elif "bid" in result:
        super_bid = result["super_bid"]
        lines.append(f"### 提交出价\n\n正常出价 {result['bid']}；超级出价 "
                     f"{super_bid if super_bid is not None else '未使用'}")
    if result.get("letters"):
        lines.append("### 私信\n\n" + "\n\n".join(f"写给 {letter['to']}：{letter['text']}"
                                                  for letter in result["letters"]))
    return lines


def make_agent(model, task: Task, letters: LetterNames | None = None):
    """letters 是这个任务可以写信的名单，不传表示不能写信。"""
    def parse(text: str) -> dict | str:
        try:
            return parse_text_output(task, text, letters)
        except OutputFormatError as exc:
            raise ModelRetry(f"{exc}\n{output_requirement(task, letters)}") from exc

    agent = Agent(model, output_type=TextOutput(parse), deps_type=AuctionDeps, retries=1)

    @agent.output_validator
    def validate(ctx: RunContext[AuctionDeps], result):
        if task in ("bid", "plan"):
            reason = validate_submission(result["bid"], result["super_bid"], ctx.deps.coins, ctx.deps.game)
            if reason:
                raise ModelRetry(f"{reason}\n请按原格式重新提交完整结果。")
        return result

    return agent


class TaskRunner:
    """在提示后追加输出格式，交给执行器运行。"""

    def __init__(self, factory: ModelFactory):
        self.executor = AgentExecutor(factory)

    def __call__(self, task: Task, prompt: str, history: list[ModelMessage], *,
                 game: GameSettings, coins: int = 0, letters: LetterNames | None = None,
                 brief_format: bool = False) -> ExecutionResult:
        """brief_format 只用于第 2 天起的出价：提示后只接一句格式提示，纠错时仍给完整格式。"""
        requirement = BRIEF_BID_FORMAT if brief_format else output_requirement(task, letters)
        # retries=1 是纠错次数；request_limit 另外限定整个任务最多两次请求。
        return self.executor(make_agent(None, task, letters), f"{prompt}\n\n{requirement}", history,
                             deps=AuctionDeps(game, coins), usage_limits=UsageLimits(request_limit=2))
