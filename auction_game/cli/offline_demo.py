"""使用假模型跑完整比赛，不调用真实服务，不收费。"""

from __future__ import annotations

import argparse
from contextlib import asynccontextmanager
from dataclasses import dataclass
import json
from pathlib import Path

from pydantic_ai.models import override_allow_model_requests
from pydantic_ai.models.function import FunctionModel

from auction_game.configuration import load_configuration, load_game_settings
from auction_game.flow.controller import Controller
from auction_game.agent.tasks import Task, TaskRunner
from auction_game.agent.text_output import LetterNames
from auction_game.game.settings import GameSettings
from auction_game.runners import make_task_runners, play_ai
from auction_game.reporting.run_output import make_run_dir
from auction_game.cli.rules import sync_root_rules
from auction_game.game.rules import render_rules


@dataclass(frozen=True)
class OfflineTask:
    """交给假模型的当前任务；letters 是可写信的名单，不能写信时为 None。"""

    task: Task
    game: GameSettings
    coins: int
    match_index: int
    day: int
    letters: LetterNames | None


def demo_submission(game, index, player_count, match_index, day, coins):
    """离线出价脚本，按局和天给出确定的出价。

    每局首日全员出 0 流局，首位玩家的超级出价落空；次日前两位玩家用超级出价狙中末位玩家的 2，
    本局赢家从这天起每天以 1 独得。正赛由前两位轮流赢（局数为奇数时最后一局给第三位），
    两人打平进入加赛，加赛由首位玩家赢。至少 4 名玩家且金币够用时这些阶段都会出现；
    否则出价截断到规则允许的范围，只保证合法。
    """
    super_bid = None
    if day == 1:
        bid, super_bid = 0, 1 if index == 0 else None
    else:
        winner = (match_index - 1) % 2
        if match_index > game.regular_matches:
            winner = 0
        elif game.regular_matches % 2 and match_index == game.regular_matches:
            winner = 2
        bid = int(index == winner)
        if day == 2:
            bid = 2 if index == player_count - 1 else bid
            super_bid = 2 if index in (0, 1) else None
    bid = min(bid, game.max_bid, coins)
    if super_bid is not None and (super_bid > game.max_bid or super_bid == bid
                                  or bid + game.super_cost > coins):
        super_bid = None
    return bid, super_bid


class OfflineTaskRunner(TaskRunner):
    """把当前任务交给这名玩家的假模型：FunctionModel 拿不到 Agent 的 deps。

    局和天按任务顺序推算：每局的规划给出第 1 天的出价，之后每次出价请求是下一天。
    离线演示遇到失败就退出、不补跑，所以每个任务只数一次。
    """

    def __init__(self, factory: DemoFactory):
        super().__init__(factory)
        self.factory = factory
        self.match_index = self.day = 0

    def __call__(self, spec, prompt, history, *, game, coins=0, letters=None):
        if spec.name == "plan":
            self.match_index, self.day = self.match_index + 1, 1
        elif spec.name == "bid":
            self.day += 1
        self.factory.current = OfflineTask(spec.name, game, coins, self.match_index, self.day, letters)
        return super().__call__(spec, prompt, history, game=game, coins=coins, letters=letters)


class OfflineController(Controller):
    """整轮任务已落盘并收集后退出，禁止进入交互补跑。"""

    def wait_for_turn(self, label, *, notices=(), require_confirmation=False):
        if require_confirmation:
            raise RuntimeError(f"离线任务失败：{label}")
        return super().wait_for_turn(label, notices=notices)


@dataclass
class DemoFactory:
    """替换模型工厂；每个任务打开一次假模型。"""

    name: str
    index: int
    settings: dict
    player_count: int = 2
    opened: int = 0
    closed: int = 0
    calls: int = 0
    signals: None = None  # 离线控制器不交互，没有暂停和取消
    current: OfflineTask | None = None  # 由这名玩家的任务入口在每个任务开始前设置
    letters_written: int = 0

    def _letter(self, letters: LetterNames | None) -> str:
        """能写信时写 1 封给配置里的下一位，最后一位写给第一位；编号便于在日志里对上送达。"""
        if letters is None:
            return ""
        self.letters_written += 1
        to = letters.recipients[self.index % len(letters.recipients)]
        return f"\n<{to}>\n模拟私信：我是{self.name}，这是我写的第 {self.letters_written} 封信。\n</{to}>"

    async def stream(self, messages, info):
        self.calls += 1
        current = self.current
        task = current.task
        if task in ("bid", "plan"):
            bid, super_bid = demo_submission(current.game, self.index, self.player_count,
                                            current.match_index, current.day, current.coins)
            strategy = f"模拟策略：当前可用 {current.coins} 金币，本次正常出价 {bid}。"
            if super_bid is not None:
                strategy += f"提交超级出价 {super_bid}，固定成本 {current.game.super_cost}；命中以实际结算为准。"
            else:
                strategy += "本次不提交超级出价，结果以公开结算为准。"
            decision = json.dumps({"bid": bid, "super_bid": super_bid}, ensure_ascii=False)
            yield (f"<strategy>\n{strategy}\n</strategy>\n<decision>\n{decision}\n</decision>"
                   + self._letter(current.letters))
        elif task == "memory":
            yield (f"我是{self.name}。本局已经结束，局末公开发言已收到。"
                   "拍卖分与命中分分别核对；下一局金币重置，按新局实际余额检查出价。")
        elif task == "speech":
            speech = f"我是{self.name}。我会根据公开结果谨慎使用金币。"
            yield ("<strategy>\n模拟私有策略：公开表达谨慎态度，不透露具体出价。\n</strategy>\n"
                   f"<speech>\n{speech}\n</speech>" + self._letter(current.letters))
        elif task == "summary":
            yield f"模拟观众总结：我是{self.name}，本场按离线脚本完成比赛。拍卖分与命中分以实际结算为准。"
        else:
            raise ValueError(f"未知离线任务：{task}")

    @asynccontextmanager
    async def open(self):
        self.opened += 1
        try:
            model = FunctionModel(stream_function=self.stream)
            model.request_errors = []
            yield model
        finally:
            self.closed += 1


def generate_demo(output: Path | None = None, *, test_model=False):
    """返回场次目录和各玩家的假模型工厂。"""
    config, configs = load_configuration(test_model=test_model)
    game = load_game_settings(test_model=test_model)
    sync_root_rules(load_game_settings())
    names = [pc.profile.name for pc in configs]
    rules = render_rules(game, names)
    factories = []

    def factory(**kwargs):
        index = len(factories)
        item = DemoFactory(names[index], index,
                           {**kwargs["model_settings"], "timeout": kwargs["timeout"],
                            "max_tokens": kwargs["max_tokens"]},
                           player_count=len(names))
        factories.append(item)
        return item

    task_runners = make_task_runners(config, configs, factory, runner=OfflineTaskRunner)
    if output is None:
        output = make_run_dir("offline")
    else:
        print(f"本场日志目录：{output}")
    print("离线演示：全员使用假模型，不调用真实服务，不收费。")
    controller = OfflineController(mode="continuous", max_concurrency=config["llm"]["max_concurrency"])
    with override_allow_model_requests(False):
        play_ai(config, configs, game, task_runners, controller,
                run_dir=output, run_type="offline", test_model=test_model, rules=rules)
    return output, factories


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test-model", action="store_true", help="用假模型验证测试配置，不调用真实服务，不收费")
    parser.add_argument("--output", type=Path, help="全新输出目录，默认 logs/<时间戳>_offline")
    args = parser.parse_args()
    generate_demo(args.output, test_model=args.test_model)


if __name__ == "__main__":
    main()
