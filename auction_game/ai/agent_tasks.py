"""个人赛的小型任务入口；Agent 负责结构与业务共用的一次输出纠错。"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai import Agent, ModelRetry, RunContext, ToolOutput
from pydantic_ai.messages import ModelMessage

from auction_game.ai.agent_execution import AgentExecutor, ExecutionResult
from auction_game.game.game_rules import GameSettings, validate_submission
from auction_game.ai.llm_client import ModelFactory

Task = Literal["bid", "plan", "speech", "summary"]


class BidOutput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", str_strip_whitespace=True)
    strategy: str = Field(min_length=1, description="仅自己可见的局势分析与出价策略")
    bid: int = Field(description="今日正常出价")
    super_bid: int | None = Field(description="今日超级出价，不使用时必须为 null")


class PlanOutput(BidOutput):
    """局前策略及本局首日提交；一次生成，首日只消费缓存。"""


class SpeechOutput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", str_strip_whitespace=True)
    strategy: str = Field(min_length=1, description="仅自己可见的发言策略")
    speech: str = Field(min_length=1, description="实际向其他玩家公开的完整发言")


@dataclass
class AuctionDeps:
    game: GameSettings | None
    coins: int


def render_task_result(result: dict | str) -> list[str]:
    """个人赛结果展示；通用日志层不解释出价与公开发言。"""
    if not isinstance(result, dict):
        return [f"### 总结\n\n{result}"]
    lines = []
    if "strategy" in result:
        lines.append(f"### 私有策略\n\n{result['strategy']}")
    if "speech" in result:
        lines.append(f"### 公开发言\n\n{result['speech']}")
    else:
        lines.append("出价结果：" + json.dumps(result, ensure_ascii=False))
    return lines


def make_agent(model, task: Task, *, settings=None):
    """组装个人赛任务；比赛参数与实时余额由业务 deps 提供。"""
    outputs = {"bid": BidOutput, "plan": PlanOutput, "speech": SpeechOutput, "summary": str}
    output = outputs[task]
    agent = Agent(model, output_type=str if output is str else ToolOutput(output, name=task),
                  deps_type=AuctionDeps, retries=1, model_settings=settings)

    @agent.output_validator
    def validate(ctx: RunContext[AuctionDeps], result):
        if isinstance(result, BidOutput):
            reason = validate_submission(result.bid, result.super_bid, ctx.deps.coins, ctx.deps.game)
            if reason:
                raise ModelRetry(reason)
        elif isinstance(result, str):
            if not result.strip():
                raise ModelRetry("观众总结正文不能为空白，请重新生成完整总结。")
            return result.strip()
        return result

    return agent


class TaskRunner:
    """个人赛适配器；业务组装后交给通用执行器，不修改输入历史。"""

    def __init__(self, factory: ModelFactory, *, capture_requests=False):
        self.executor = AgentExecutor(factory, capture_requests=capture_requests)

    def __call__(self, task: Task, prompt: str, history: list[ModelMessage], *,
                 game: GameSettings | None = None, coins: int = 0) -> ExecutionResult[BidOutput | SpeechOutput | str]:
        if task in ("bid", "plan") and game is None:
            raise ValueError("出价及规划任务需要比赛参数")
        return self.executor(make_agent(None, task), prompt, history, deps=AuctionDeps(game, coins))
