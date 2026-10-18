"""个人赛的小型任务入口；Agent 负责结构与业务共用的一次输出纠错。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_core import PydanticCustomError
from pydantic_ai import Agent, ModelRetry, RunContext, ToolOutput
from pydantic_ai.messages import ModelMessage
from pydantic_ai.usage import UsageLimits

from auction_game.ai.agent_execution import AgentExecutor, ExecutionResult
from auction_game.game.game_rules import GameSettings, validate_submission
from auction_game.ai.llm_client import ModelFactory
from auction_game.ai import prompts

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
    memory: str = Field(min_length=1, description="累计私有笔记，整体替换旧记忆")


# 唯一任务到输出工具映射；自然语言提醒也从此取得当前名称。
OUTPUT_TOOLS = {
    "bid": ToolOutput(BidOutput, name="bid", description="提交今日出价"),
    "speech": ToolOutput(SpeechOutput, name="speech", description="提交公开发言"),
    "memory": ToolOutput(MemoryOutput, name="save_memory", description="保存累计私有记忆"),
}
OUTPUT_TOOLS["plan"] = OUTPUT_TOOLS["bid"]


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
    agent = Agent(model, output_type=str if task == "summary" else OUTPUT_TOOLS[task],
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
                 game: GameSettings | None = None, coins: int = 0) -> ExecutionResult[BidOutput | SpeechOutput | MemoryOutput | str]:
        if task in ("bid", "plan") and game is None:
            raise ValueError("出价及规划任务需要比赛参数")
        tool = OUTPUT_TOOLS.get(task)
        prompt = prompts.with_output_requirement(prompt, tool.name if tool else None)
        # SDK 的文本与工具校验路径可能分别计数；公开请求上限保证混合纠错也共用一次。
        return self.executor(make_agent(None, task), prompt, history, deps=AuctionDeps(game, coins),
                             usage_limits=UsageLimits(request_limit=2))
