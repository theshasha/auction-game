"""使用假模型跑完整比赛，不调用真实服务，不收费。"""

from __future__ import annotations

import argparse
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from copy import deepcopy
import json
from pathlib import Path

from pydantic_ai.models import override_allow_model_requests
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai.messages import ModelMessagesTypeAdapter, UserPromptPart
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_core import to_jsonable_python

from auction_game.configuration import channel_model_profile, load_configuration, load_game_settings
from auction_game.controller import Controller
from auction_game.ai.agent_execution import CallFailedError
from auction_game.ai.agent_tasks import AuctionDeps, TaskRunner
from auction_game.reporting.replay import export_replay, capture_protocol_definition
from auction_game.reporting.tool_records import current_tool_task
from auction_game.reporting.request_records import digest
from auction_game.ai.llm_client import ModelFactory
from auction_game.runners import run_ai
from auction_game.reporting.run_output import make_run_dir
from auction_game.game.rules import sync_root_rules


_task_deps: ContextVar[AuctionDeps] = ContextVar("offline_auction_deps")


class OfflineToolAudit(WrapperModel):
    """FunctionModel 没有 HTTP；用生产 SDK 离线序列化并明确标注来源。"""

    def __init__(self, wrapped, factory):
        super().__init__(wrapped)
        self.factory = factory
        self.request_errors = []
        self.request_number = 0

    @asynccontextmanager
    async def request_stream(self, messages, model_settings, model_request_parameters, run_context=None):
        self.request_number += 1
        task = current_tool_task.get()
        if task is not None:
            record = {"messages": ModelMessagesTypeAdapter.dump_python(deepcopy(messages), mode="json"),
                      "settings": to_jsonable_python(model_settings),
                      "parameters": to_jsonable_python(asdict(model_request_parameters))}
            key = digest([record["settings"], record["parameters"]])
            if key not in self.factory.tool_definitions:
                sdk_factory = ModelFactory(self.factory.protocol, self.factory.model, self.factory.base_url,
                                           "offline-not-a-secret", 3, self.factory.settings["max_tokens"],
                                           model_profile=self.factory.model_profile)
                snapshot = await capture_protocol_definition(sdk_factory, record)
                self.factory.tool_definitions[key] = snapshot["definition"]
            task.record(self.factory.protocol, self.factory.model, self.factory.tool_definitions[key],
                        request=self.request_number, attempt=1, source="offline_sdk_generated")
        async with self.wrapped.request_stream(messages, model_settings, model_request_parameters, run_context) as stream:
            yield stream


class OfflineTaskRunner(TaskRunner):
    """仅离线使用：显式转交实时任务依赖，失败直接退出，禁止等待无人确认。"""

    def __call__(self, task, prompt, history, *, game=None, coins=0):
        token = _task_deps.set(AuctionDeps(game, coins))
        try:
            return super().__call__(task, prompt, history, game=game, coins=coins)
        except CallFailedError as exc:
            raise RuntimeError(f"离线任务失败：{self.executor.factory.name} / {task}：{exc}") from exc
        finally:
            _task_deps.reset(token)


@dataclass
class DemoFactory:
    """只替换模型；每任务创建并关闭流，SDK 仅离线序列化，不读取环境密钥。"""

    name: str
    index: int
    game: object
    settings: dict
    model_profile: dict
    api_key: str = field(default="offline-demo-not-a-secret", repr=False)
    opened: int = 0
    closed: int = 0
    calls: int = 0
    protocol: str = "chat"
    model: str = "offline"
    base_url: str = "https://offline.invalid/v1"
    tool_definitions: dict = field(default_factory=dict)

    async def stream(self, messages, info):
        self.calls += 1
        tool = info.output_tools[0].name if info.output_tools else "summary"
        if tool == "bid":
            deps = _task_deps.get()
            bid = min(3, deps.game.max_bid, deps.coins) if self.index == 0 else 0
            strategy = (
                f"模拟策略：以 {bid} 枚金币争取唯一最高价，保留工资支撑后续出价；"
                "本演示的其他玩家固定零出价，可据昨日公开结果核对。"
                if self.index == 0 else
                "模拟策略：固定零出价保存金币；这可能与其他零价玩家撞车，本例用于展示合法结算。"
            )
            result = {"strategy": strategy, "bid": bid, "super_bid": None}
        elif tool == "save_memory":
            result = {"memory": f"我是{self.name}。观察：首位玩家持续正价，其余固定零价；局末大家公开表示谨慎。"
                                "推测：固定零价会继续撞车，需后续验证。下一局复核公开结果，避免机械照搬。"}
        elif tool == "speech":
            prompt = next(part.content for message in reversed(messages) for part in reversed(message.parts)
                          if isinstance(part, UserPromptPart))
            if "最终感言" in prompt:
                speech = f"我是{self.name}。比赛已经结束，最终成绩以公布的排名为准；谢谢大家让我看清了金币分配的重要性。"
            elif "局末发言" in prompt:
                speech = f"我是{self.name}。这一局的出价与胜场已经公开，我会据此检查资源使用，下一局继续谨慎参与。"
            else:
                speech = f"我是{self.name}。这场比赛我会观察公开结果，谨慎使用金币。"
            result = {"strategy": "模拟私有策略：公开表达谨慎态度，不透露具体出价。",
                      "speech": speech}
        else:
            yield f"模拟观众总结：我是{self.name}，本场按固定策略完成比赛。胜场来自实际结算，零价撞车说明只节省金币也有代价。"
            return
        yield {0: DeltaToolCall(name=tool, json_args=json.dumps(result, ensure_ascii=False),
                               tool_call_id=f"demo-{self.index}-{self.calls}")}

    @asynccontextmanager
    async def open(self):
        self.opened += 1
        try:
            model = FunctionModel(stream_function=self.stream)
            yield OfflineToolAudit(model, self)
        finally:
            self.closed += 1


def generate_demo(output: Path | None = None, player: str | None = None, *, test_model=False):
    """返回回放路径及模型资源计数；工具定义用假密钥离线生成，不发真实请求。"""
    loaded_configuration = load_configuration(test_model=test_model)
    _, configs = loaded_configuration
    game = load_game_settings()
    sync_root_rules(game)
    names = [pc["name"] for pc in configs]
    player = player or names[0]
    if player not in names:
        raise ValueError(f"未知玩家 {player}，可选：{'、'.join(names)}")
    # 防止重复追加旧审计；不覆盖或删除已有生成物。
    if output is not None:
        output = output.resolve()
        if output.exists():
            raise FileExistsError(f"场次已存在，拒绝覆盖：{output}")
        print(f"本场日志目录：{output}")
    else:
        output = make_run_dir("offline")
    factories = []

    def factory(**kwargs):
        index = len(factories)
        item = DemoFactory(names[index], index, game,
                           {**kwargs["model_settings"], "timeout": kwargs["timeout"],
                             "max_tokens": kwargs["max_tokens"]},
                           channel_model_profile(kwargs["channel"], kwargs["protocol"]),
                           protocol=kwargs["protocol"], model=kwargs["model"],
                           base_url=kwargs["channel"]["base_url"])
        factories.append(item)
        return item

    controller = Controller(mode="continuous")
    with override_allow_model_requests(False):
        status = run_ai(model_factory=factory, run_dir=output, controller=controller,
                          record_player=player, start_reader=False, test_model=test_model, run_type="offline",
                          loaded_configuration=loaded_configuration, prepared_game=game,
                          task_runner_factory=OfflineTaskRunner)
    if status:
        raise RuntimeError(f"离线比赛启动失败：{status}")
    replay = export_replay(output, player, capture_definitions=True)
    return replay, factories


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--player", help="回放玩家，默认 players.yaml 第一位")
    parser.add_argument("--test-model", action="store_true", help="用假模型验证测试配置，不调用真实服务，不收费")
    parser.add_argument("--output", type=Path, help="全新输出目录，默认 logs/<时间戳>_offline")
    args = parser.parse_args()
    replay, _ = generate_demo(args.output, args.player, test_model=args.test_model)
    print(f"\n回放：{replay}")


if __name__ == "__main__":
    main()
