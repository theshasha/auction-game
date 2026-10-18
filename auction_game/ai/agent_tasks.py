"""个人赛的小型任务入口；Agent 负责结构与业务共用的一次输出纠错。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from pydantic_core import PydanticCustomError
from pydantic_ai import Agent, ModelRetry, RunContext, TextOutput
from pydantic_ai.messages import ModelMessage
from pydantic_ai.usage import UsageLimits

from auction_game.ai.agent_execution import AgentExecutor, ExecutionResult
from auction_game.game.game_rules import GameSettings, validate_submission
from auction_game.ai.llm_client import ModelFactory
from auction_game.ai import prompts
from auction_game.ai.text_output import OutputFormatError, output_requirement, parse_text_output

Task = Literal["bid", "plan", "speech", "memory", "summary"]


class BidOutput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", str_strip_whitespace=True)
    strategy: str = Field(min_length=1, description="仅自己可见的局势分析与出价策略")
    bid: int = Field(description="今日正常出价，必须为不加引号的整数")
    super_bid: int | None = Field(description="必填：今日超级出价，使用不加引号的整数；不使用时填 JSON null")

    @field_validator("super_bid", mode="before")
    @classmethod
    def validate_super_bid_type(cls, value):
        if type(value) is str and value == "null":
            return None
        if value is not None and type(value) is not int:
            raise PydanticCustomError("super_bid_type",
                                      "super_bid 必填，必须为不加引号的整数或 JSON null。")
        return value


PlanOutput = BidOutput  # 业务阶段仍为 plan；与每日出价共用完整输出定义。


class SpeechOutput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", str_strip_whitespace=True)
    strategy: str = Field(min_length=1, description="仅自己可见的发言策略")
    speech: str = Field(min_length=1, description="实际向其他玩家公开的完整发言")


class MemoryOutput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", str_strip_whitespace=True)
    memory: str = Field(min_length=1, description="供下一局使用的私人笔记；本次内容会替换旧记忆")


OUTPUT_MODELS = {"bid": BidOutput, "plan": PlanOutput, "speech": SpeechOutput, "memory": MemoryOutput}


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
    if "memory" in result:
        lines.append(f"### 累计私有记忆\n\n{result['memory']}")
    if "speech" in result:
        lines.append(f"### 公开发言\n\n{result['speech']}")
    elif "bid" in result:
        super_bid = result["super_bid"]
        lines.append(f"### 提交出价\n\n正常出价 {result['bid']}；超级出价 "
                     f"{super_bid if super_bid is not None else '未使用'}")
    return lines


def make_agent(model, task: Task, *, settings=None):
    """组装个人赛任务；比赛参数与实时余额由业务 deps 提供。"""
    def parse(text: str) -> BidOutput | SpeechOutput | MemoryOutput | str:
        try:
            result = parse_text_output(task, text)
            return OUTPUT_MODELS[task].model_validate(result) if task in OUTPUT_MODELS else result
        except (OutputFormatError, ValidationError) as exc:
            if isinstance(exc, ValidationError):
                reason = "；".join(f"{'.'.join(map(str, error['loc']))}: {error['msg']}"
                                  for error in exc.errors(include_input=False, include_context=False))
            else:
                reason = str(exc)
            raise ModelRetry(f"{reason}\n请重新提交完整结果。\n{output_requirement(task)}") from exc

    agent = Agent(model, output_type=TextOutput(parse),
                  deps_type=AuctionDeps, retries=1, model_settings=settings)

    @agent.output_validator
    def validate(ctx: RunContext[AuctionDeps], result):
        if isinstance(result, BidOutput):
            reason = validate_submission(result.bid, result.super_bid, ctx.deps.coins, ctx.deps.game)
            if reason:
                raise ModelRetry(f"{reason}\n请按原格式重新提交完整结果。")
        return result

    return agent


class TaskRunner:
    """个人赛适配器；业务组装后交给通用执行器，不修改输入历史。"""

    def __init__(self, factory: ModelFactory, *, capture_requests=False):
        self.executor = AgentExecutor(factory, capture_requests=capture_requests)

    def __call__(self, task: Task, prompt: str, history: list[ModelMessage], *,
                 game: GameSettings | None = None, coins: int = 0) -> ExecutionResult[BidOutput | SpeechOutput | MemoryOutput | str]:
        if task in ("bid", "plan") and game is None:
            raise ValueError("出价及规划任务需要比赛参数")
        prompt = prompts.with_output_requirement(prompt, task)
        # 格式解析与业务校验共用一次纠错，额外用请求上限守住整个任务预算。
        return self.executor(make_agent(None, task), prompt, history, deps=AuctionDeps(game, coins),
                             usage_limits=UsageLimits(request_limit=2))
