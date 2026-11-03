"""测试工厂：数据对象、引擎场景和 AI 玩家；pytest 夹具在 tests/conftest.py。"""

from copy import deepcopy
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Callable, Optional
import json

from pydantic_ai.models.function import FunctionModel
from auction_game.agent.tasks import TaskRunner

from auction_game.agent.player import AIPlayer
from auction_game.flow.controller import Controller
from auction_game.game.engine import compute_standings, run_match, settle_day
from auction_game.game.settings import GameSettings
from auction_game.game.models import MatchLog, Observation
from auction_game.game.players import Player

NAMES = ["甲", "乙", "丙", "丁", "戊", "己"]
TEST_GAME = GameSettings(
    initial_coins=7, daily_salary=3, daily_pool=(4,) * 6, max_bid=7,
    super_cost=2, super_bounty=3, super_hit_score=2,
    regular_matches=5, max_playoffs=20,
)

CHANNELS = {
    "hub": {"base_url": "https://offline.invalid/v1", "api_key_env": "FAKE_KEY"},
    "anthropic": {"base_url": "https://offline.invalid/anthropic", "api_key_env": "FAKE_KEY"},
}


# ---------- 数据对象 ----------

def make_day(day, bids, pool=1, super_bids=None, game=TEST_GAME):
    """用引擎结算一天。bids 是按 NAMES 顺序的正常出价，人数可少于六人；
    super_bids 是 {玩家: 超级出价}。获胜、撞车和命中都由引擎判定。"""
    super_bids = super_bids or {}
    return settle_day(day, pool, [(name, bid, super_bids.get(name)) for name, bid in zip(NAMES, bids)], game)


def make_log(match_index=1, is_playoff=False, days=None, auction_scores=None, game=TEST_GAME, *,
             scores=None, remaining_coins=None):
    """局日志；分数和余额由调用方填写，默认全员 0。"""
    players = auction_scores if auction_scores is not None else NAMES
    return MatchLog(
        match_index=match_index,
        is_playoff=is_playoff,
        game=game,
        days=days if days is not None else [make_day(1, [5, 4, 3, 2, 1, 0], game=game)],
        auction_scores=auction_scores if auction_scores is not None else {name: 0 for name in NAMES},
        scores=scores if scores is not None else {name: 0 for name in players},
        remaining_coins=remaining_coins if remaining_coins is not None else {name: 0 for name in players},
    )


def make_logs(count, game=TEST_GAME):
    """已结束的前 count 局，都是正赛局。"""
    return [make_log(match_index=index, game=game) for index in range(1, count + 1)]


def make_standings(auction_scores_history: dict[str, list[int]], *, scores_history=None):
    """通过生产路径排名；不给 scores_history 时总分只含拍卖分。"""
    if scores_history is None:
        scores_history = {name: list(scores) for name, scores in auction_scores_history.items()}
    return compute_standings(auction_scores_history, scores_history=scores_history)


def make_obs(my_name="甲", day=1, today_history=None, total_auction_score=0, pool=1, coins=7,
             all_coins=None, match_index=1, game=TEST_GAME, *, match_score=0, total_score=0):
    """全字段 Observation 工厂，today_history 用 make_day 生成。

    和引擎一样总给全员排名；有昨日结果时默认给全员余额。
    """
    names = [my_name, *(name for name in NAMES if name != my_name)]
    if all_coins is None and today_history:
        all_coins = {name: coins for name in names}
    return Observation(
        my_name=my_name,
        my_coins=coins,
        my_match_auction_score=0,
        my_total_auction_score=total_auction_score,
        my_match_score=match_score,
        my_total_score=total_score,
        match_index=match_index,
        day=day,
        today_pool=pool,
        today_history=today_history or [],
        standings=make_standings({name: [total_auction_score if name == my_name else 0] for name in names},
                                 scores_history={name: [total_score if name == my_name else 0] for name in names}),
        game=game,
        all_coins=all_coins,
    )


# ---------- 引擎场景：策略只依赖 obs.day 和 obs.my_name，全员可共用 ----------

class ScriptedPlayer(Player):
    """按给定函数出价：strategy(obs) 返回正常出价，可选的 super_strategy(obs) 返回超级出价或 None。"""

    def __init__(
        self,
        name: str,
        strategy: Callable[[Observation], int],
        super_strategy: Optional[Callable[[Observation], Optional[int]]] = None,
    ):
        super().__init__(name)
        self.strategy = strategy
        self.super_strategy = super_strategy

    def decide_bid(self, obs: Observation) -> tuple[int, Optional[int]]:
        super_bid = None
        if self.super_strategy is not None:
            super_bid = self.super_strategy(obs)
        return self.strategy(obs), super_bid


def run_one_match(strategies, match_index=1, is_playoff=False, super_strategies=None,
                  game=TEST_GAME):
    """用给定的 {名字: 策略函数} 跑一局，返回 MatchLog。super_strategies 可选。"""
    super_strategies = super_strategies or {}
    players = [
        ScriptedPlayer(name, strategies[name], super_strategies.get(name)) for name in NAMES
    ]
    auction_scores_history = {name: [] for name in NAMES}
    return run_match(
        players, match_index, is_playoff, auction_scores_history, game,
        scores_history={name: [] for name in NAMES},
    )


def constant_by_name(bids_by_name: dict):
    """每人每天固定出价：{名字: 出价}。"""
    return {name: (lambda b: lambda obs: b)(bid) for name, bid in bids_by_name.items()}


def constant_strategies(bids: list[int]):
    """每人每天固定出同一个价：zip(NAMES, bids)。"""
    return constant_by_name(dict(zip(NAMES, bids)))


def bids_by_day(table, default=0, capture=None):
    """按天出价表策略：table 为 {天: [六人出价] 或 {名字: 出价}}，未列出的天出 default。
    capture 传入 dict 时按名字记录每天看到的 Observation（capture[名字][天-1]）。
    策略只依赖 obs.day / obs.my_name，全员可共用同一个返回函数。"""
    def strategy(obs):
        if capture is not None:
            capture.setdefault(obs.my_name, []).append(obs)
        day_bids = table.get(obs.day)
        if day_bids is None:
            return default
        if isinstance(day_bids, dict):
            return day_bids[obs.my_name]
        return dict(zip(NAMES, day_bids))[obs.my_name]
    return strategy


def super_by_day(table):
    """按天超级出价：{天: 数值}，未列出的天不使用。返回 super_strategy。"""
    return lambda obs: table.get(obs.day)


def run_scenario(table, default=0, super_strategies=None, capture=None, game=TEST_GAME):
    """最常见场景：全员共用一张按天出价表（表内按名字区分出价），跑一局返回 MatchLog。"""
    strategy = bids_by_day(table, default=default, capture=capture)
    return run_one_match(
        {name: strategy for name in NAMES}, super_strategies=super_strategies, game=game
    )


# ---------- AI 玩家：只替换模型输出，解析、校验和纠错走生产 Agent ----------

ALL_NAMES = ["Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot"]
TEST_PREDICTIONS = [(name, 10) for name in ALL_NAMES]


def fake_task_runner(script):
    """按脚本依次给出模型回复；dict 写成文本格式，异常直接抛出，可调用项按收到的消息生成回复。"""
    queue = list(script)
    calls = []

    async def stream(messages, info):
        calls.append(deepcopy(messages))
        item = queue.pop(0)
        if callable(item):
            item = item(messages)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, dict):
            yield text_reply(item)
        else:
            yield item

    @asynccontextmanager
    async def open_model():
        model = FunctionModel(stream_function=stream)
        model.request_errors = []
        yield model

    runner = TaskRunner(SimpleNamespace(open=open_model, settings={}, signals=None))
    runner.calls = calls
    return runner


def request_text(runner, index):
    """fake_task_runner 第 index 次请求里本次新发的用户提示。"""
    return runner.calls[index][-1].parts[-1].content


def text_reply(data):
    """将测试字段写成模型文本；保留非法字段供生产解析器检查。letters 写成私信块，不进 decision。"""
    if isinstance(data, str):
        return data
    if "memory" in data:
        return data["memory"]
    strategy = data.get("strategy", "")
    letters = "".join(f"\n<{letter['to']}>\n{letter['text']}\n</{letter['to']}>"
                      for letter in data.get("letters", []))
    if "speech" in data:
        return f"<strategy>\n{strategy}\n</strategy>\n<speech>\n{data['speech']}\n</speech>{letters}"
    decision = {key: value for key, value in data.items() if key not in ("strategy", "letters")}
    return (f"<strategy>\n{strategy}\n</strategy>\n<decision>\n"
            f"{json.dumps(decision, ensure_ascii=False)}\n</decision>{letters}")


def make_player(task_runner, tmp_path, game=TEST_GAME, name="Alpha"):
    """名单是 ALL_NAMES，name 要在其中。"""
    return AIPlayer(name, "测试人设", "测试人设正文", TEST_PREDICTIONS, ALL_NAMES,
                    task_runner, game=game, log_path=tmp_path / f"ai_{name}.jsonl",
                    readable_path=tmp_path / f"ai_{name}.md")


def continuous_controller():
    """连续模式的控制器。控制器要等用户按键时直接让测试失败，报错里带上失败公告；
    否则任务失败后一直等确认，测试卡住。"""
    messages = []

    def announce(text):
        messages.append(text)
        if text.startswith("[单步]"):
            raise AssertionError("\n".join(messages))

    return Controller(mode="continuous", announce=announce)


def task_entries(path):
    """读玩家 JSONL 里的任务记录，跳过开头的系统提示。"""
    entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return [entry for entry in entries if entry["task"] != "system"]


def bid_reply(bid, super_bid=None, strategy="我的私有分析"):
    return {"strategy": strategy, "bid": bid, "super_bid": super_bid}


def speech_reply(speech):
    return {"strategy": "先放个烟雾弹（私有）", "speech": speech}


def make_ai_obs(day=1, coins=6, game=TEST_GAME):
    """Alpha 视角的 Observation。"""
    return make_obs(my_name="Alpha", day=day, coins=coins, game=game)
