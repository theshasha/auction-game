"""六种任务的规格和输出：解析文本输出、校验出价，格式错误和违规出价共用一次纠错。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic_ai import Agent, ModelRetry, RunContext, TextOutput
from pydantic_ai.messages import ModelMessage
from pydantic_ai.usage import UsageLimits

from auction_game.llm.execution import AgentExecutor, ExecutionResult
from auction_game.game.settings import GameSettings, validate_submission
from auction_game.llm.client import ModelFactory
from auction_game.agent.text_output import (BOARD, DECISION, SPEECH, Blocks, LetterScope, OutputFormatError,
                                            output_requirement, parse_text_output)

Task = Literal["bid", "plan", "speech", "discussion", "memory", "summary"]


def _strategy_and_letters(strategy: str, letters: list[dict], result: str) -> list[str]:
    """分块任务在私人日志里的小节：私有策略、结果，有信时再加私信或队内信件区的信。"""
    lines = [f"### 私有策略\n\n{strategy}", result]
    if letters and letters[0]["to"] == BOARD:
        lines.append(f"### {BOARD}\n\n{letters[0]['text']}")
    elif letters:
        lines.append("### 私信\n\n" + "\n\n".join(f"写给 {letter['to']}：{letter['text']}" for letter in letters))
    return lines


@dataclass(frozen=True)
class BidOutput:
    """出价和规划的结果；规划交的是本局第 1 天的出价。

    解析出的 bid、super_bid 可能是任意 JSON 值，通过出价校验后才交给调用方。
    """

    strategy: str
    bid: int
    super_bid: int | None
    letters: list[dict]

    @classmethod
    def from_parsed(cls, blocks: Blocks) -> BidOutput:
        return cls(blocks.strategy, blocks.body["bid"], blocks.body["super_bid"], blocks.letters)

    @property
    def submission(self) -> tuple[int, int | None]:
        return self.bid, self.super_bid

    def to_log(self) -> dict:
        return {"strategy": self.strategy, "bid": self.bid, "super_bid": self.super_bid, "letters": self.letters}

    @classmethod
    def from_log(cls, data: dict) -> BidOutput:
        return cls(**data)

    def render(self) -> list[str]:
        super_bid = self.super_bid if self.super_bid is not None else "未使用"
        return _strategy_and_letters(self.strategy, self.letters,
                                     f"### 提交出价\n\n正常出价 {self.bid}；超级出价 {super_bid}")


@dataclass(frozen=True)
class SpeechOutput:
    strategy: str
    speech: str
    letters: list[dict]

    @classmethod
    def from_parsed(cls, blocks: Blocks) -> SpeechOutput:
        return cls(blocks.strategy, blocks.body, blocks.letters)

    def to_log(self) -> dict:
        return {"strategy": self.strategy, "speech": self.speech, "letters": self.letters}

    @classmethod
    def from_log(cls, data: dict) -> SpeechOutput:
        return cls(**data)

    def render(self) -> list[str]:
        return _strategy_and_letters(self.strategy, self.letters, f"### 公开发言\n\n{self.speech}")


@dataclass(frozen=True)
class DiscussionOutput:
    """开局讨论一轮的结果：私有策略和必须写的一封信。"""

    strategy: str
    letter: str

    @classmethod
    def from_parsed(cls, blocks: Blocks) -> DiscussionOutput:
        return cls(blocks.strategy, blocks.body)

    @property
    def letters(self) -> list[dict]:
        return [{"to": BOARD, "text": self.letter}]

    def to_log(self) -> dict:
        return {"strategy": self.strategy, "letters": self.letters}

    @classmethod
    def from_log(cls, data: dict) -> DiscussionOutput:
        return cls(data["strategy"], data["letters"][0]["text"])

    def render(self) -> list[str]:
        return [f"### 私有策略\n\n{self.strategy}", f"### {BOARD}\n\n{self.letter}"]


@dataclass(frozen=True)
class MemoryOutput:
    memory: str
    letters: tuple = ()  # 整理记忆不能写信

    @classmethod
    def from_parsed(cls, body: str) -> MemoryOutput:
        return cls(body)

    def to_log(self) -> dict:
        return {"memory": self.memory}

    @classmethod
    def from_log(cls, data: dict) -> MemoryOutput:
        return cls(data["memory"])

    def render(self) -> list[str]:
        return [f"### 累计私有记忆\n\n{self.memory}"]


@dataclass(frozen=True)
class SummaryOutput:
    text: str
    letters: tuple = ()  # 观众总结不能写信

    @classmethod
    def from_parsed(cls, body: str) -> SummaryOutput:
        return cls(body)

    def to_log(self) -> str:
        return self.text

    @classmethod
    def from_log(cls, data: str) -> SummaryOutput:
        return cls(data)

    def render(self) -> list[str]:
        return [f"### 总结\n\n{self.text}"]


TaskOutput = BidOutput | SpeechOutput | DiscussionOutput | MemoryOutput | SummaryOutput


@dataclass(frozen=True)
class TaskSpec:
    """一种任务的格式、校验和结果去向。

    second 是 strategy 之后那一块，None 表示直接返回正文；decision 块的出价要通过出价校验。
    history 是结果写回对话的方式：append 追加，reset 用记忆替换之前的对话，none 不写回。
    output 的 from_log 是 to_log 的反向，续跑时从任务日志还原结果。
    """

    name: Task
    second: Literal["decision", "speech", "队内信件区"] | None
    output: type
    can_write_letters: bool
    history: Literal["append", "reset", "none"]


TASKS: dict[str, TaskSpec] = {
    "bid": TaskSpec("bid", DECISION, BidOutput, True, "append"),
    "plan": TaskSpec("plan", DECISION, BidOutput, True, "append"),
    "speech": TaskSpec("speech", SPEECH, SpeechOutput, True, "append"),
    "discussion": TaskSpec("discussion", BOARD, DiscussionOutput, False, "append"),
    "memory": TaskSpec("memory", None, MemoryOutput, False, "reset"),
    "summary": TaskSpec("summary", None, SummaryOutput, False, "none"),
}


@dataclass
class AuctionDeps:
    game: GameSettings
    coins: int


def make_agent(model, spec: TaskSpec, letters: LetterScope | None = None):
    """letters 是这个任务的写信范围，不传表示不能写信。"""
    def parse(text: str) -> TaskOutput:
        try:
            return spec.output.from_parsed(parse_text_output(spec.second, text, letters))
        except OutputFormatError as exc:
            raise ModelRetry(f"{exc}\n{output_requirement(spec.second, letters)}") from exc

    agent = Agent(model, output_type=TextOutput(parse), deps_type=AuctionDeps, retries=1)

    @agent.output_validator
    def validate(ctx: RunContext[AuctionDeps], result: TaskOutput) -> TaskOutput:
        if spec.second == DECISION:
            reason = validate_submission(result.bid, result.super_bid, ctx.deps.coins, ctx.deps.game)
            if reason:
                raise ModelRetry(f"{reason}\n请按原格式重新提交完整结果。")
        return result

    return agent


def request_prompt(spec: TaskSpec, prompt: str, letters: LetterScope | None = None, brief: str | None = None) -> str:
    """发给模型的完整请求文本：提示后附输出格式，brief 不为空时只附这一句。续跑用它比对日志里的输入。"""
    return f"{prompt}\n\n{brief or output_requirement(spec.second, letters)}"


class TaskRunner:
    """在提示后追加输出格式，交给执行器运行。"""

    def __init__(self, factory: ModelFactory):
        self.executor = AgentExecutor(factory)

    def __call__(self, spec: TaskSpec, prompt: str, history: list[ModelMessage], *, game: GameSettings,
                 coins: int = 0, letters: LetterScope | None = None, brief: str | None = None) -> ExecutionResult:
        """brief 不为空时，请求后只附这一句格式提示，纠错时仍给完整格式。"""
        # retries=1 是纠错次数；request_limit 另外限定整个任务最多两次请求。
        return self.executor(make_agent(None, spec, letters), request_prompt(spec, prompt, letters, brief), history,
                             deps=AuctionDeps(game, coins), usage_limits=UsageLimits(request_limit=2))
