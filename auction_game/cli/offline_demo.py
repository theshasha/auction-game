"""确定性离线完整个人赛；生产组装与测试共用，模型边界零网络。"""

from __future__ import annotations

import argparse
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import json
from pathlib import Path

from pydantic_ai.models import override_allow_model_requests
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai.messages import UserPromptPart

from auction_game.configuration import channel_model_profile, load_configuration, load_game_settings
from auction_game.controller import Controller
from auction_game.reporting.replay import export_replay
from auction_game.runners import run_ai
from auction_game.reporting.run_output import make_run_dir
from auction_game.game.rules import sync_root_rules


@dataclass
class DemoFactory:
    """只替换模型；每任务仍创建、关闭流，不访问 SDK 或环境密钥。"""

    name: str
    index: int
    game: object
    settings: dict
    model_profile: dict
    api_key: str = field(default="offline-demo-not-a-secret", repr=False)
    opened: int = 0
    closed: int = 0
    calls: int = 0

    async def stream(self, messages, info):
        self.calls += 1
        tool = info.output_tools[0].name if info.output_tools else "summary"
        if tool in ("bid", "plan"):
            bid = min(3, self.game.max_bid) if self.index == 0 else 0
            strategy = (
                f"模拟策略：以 {bid} 枚金币争取唯一最高价，保留工资支撑后续出价；"
                "本演示的其他玩家固定零出价，可据昨日公开结果核对。"
                if self.index == 0 else
                "模拟策略：固定零出价保存金币；这可能与其他零价玩家撞车，本例用于展示合法结算。"
            )
            result = {"strategy": strategy, "bid": bid, "super_bid": None}
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
            model.request_errors = []
            yield model
        finally:
            self.closed += 1


def generate_demo(output: Path | None = None, player: str | None = None, *, test_model=False):
    """返回回放路径及模型资源计数；赛后另以假密钥离线捕获 SDK 工具定义。"""
    _, configs = load_configuration(test_model=test_model)
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
    else:
        output = make_run_dir("offline")
    factories = []

    def factory(**kwargs):
        index = len(factories)
        item = DemoFactory(names[index], index, game,
                           {**kwargs["model_settings"], "timeout": kwargs["timeout"],
                             "max_tokens": kwargs["max_tokens"]},
                           channel_model_profile(kwargs["channel"], kwargs["protocol"]))
        factories.append(item)
        return item

    controller = Controller()
    controller.offer_command("c")
    with override_allow_model_requests(False):
        status = run_ai(model_factory=factory, run_dir=output, controller=controller,
                         record_player=player, start_reader=False, test_model=test_model, run_type="offline")
    if status:
        raise RuntimeError(f"离线比赛启动失败：{status}")
    replay = export_replay(output, player, simulated=True, capture_definitions=True)
    return replay, factories


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--player", help="回放玩家，默认 players.yaml 第一位")
    parser.add_argument("--test-model", action="store_true", help="离线验证整套测试模型覆盖；仍不请求真实服务")
    parser.add_argument("--output", type=Path, help="全新输出目录，默认 logs/<时间戳>_offline")
    args = parser.parse_args()
    replay, _ = generate_demo(args.output, args.player, test_model=args.test_model)
    print(f"\n回放：{replay}")


if __name__ == "__main__":
    main()
