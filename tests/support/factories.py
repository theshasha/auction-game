"""普通测试工厂；pytest 夹具在 tests/conftest.py。

分三节：
1. 通用数据对象工厂：NAMES / make_day / make_log / make_standings / history / make_obs
   （DayResult 用 super_uses + collision_values，public_view 含 bids_detail；
   Observation 含 my_last_sniped / my_last_super_hit / all_coins）。
2. 引擎场景构造器：run_one_match / constant_strategies / constant_by_name /
   bids_by_day / super_by_day / run_scenario——消灭「按天查表策略」在各用例里
   逐字重写的样板。策略只依赖 obs.day 与 obs.my_name，可全员共用同一个函数。
3. AI 玩家测试夹具：模型边界注入，任务均经过生产 Agent 校验。
"""

from copy import deepcopy
from contextlib import asynccontextmanager
from types import SimpleNamespace
import json

from pydantic_ai.models.function import FunctionModel
from auction_game.ai.agent_tasks import TaskRunner

from auction_game.ai.ai_player import AIPlayer
from auction_game.game.engine import compute_standings, run_match
from auction_game.game.game_rules import GameSettings
from auction_game.game.models import BidRecord, DayResult, MatchLog, Observation
from auction_game.game.players import ScriptedPlayer

NAMES = ["甲", "乙", "丙", "丁", "戊", "己"]
TEST_GAME = GameSettings(
    initial_coins=7, daily_salary=3, daily_pool=(4,) * 6, max_bid=7,
    super_cost=2, super_bounty=3, super_hit_score=2,
    regular_matches=5, max_playoffs=20,
)

CHANNELS = {
    "hub": {"base_url": "https://offline.invalid/v1", "api_key_env": "FAKE_KEY",
            "responses_profile": {"supports_forced_tool_choice": False}},
    "anthropic": {"base_url": "https://offline.invalid/anthropic", "api_key_env": "FAKE_KEY"},
}




# ---------- 1. 通用数据对象工厂 ----------

def make_day(day, bids, pool, winner, winning_bid, collision_values, auction_score=0,
             is_void=False, super_bids=None):
    """构造一天结果。bids 为六人正常出价；super_bids 为 {玩家: 数值} 或
    {玩家: (数值, 是否命中)}，每个使用者在出价表里追加一笔 is_super=True
    的记录，并在 super_uses 里登记 {"player", "value", "hit"}。
    collision_values 为当天撞车价位的具体数值（升序）。"""
    records = [BidRecord(player=name, raw_bid=bid, bid=bid) for name, bid in zip(NAMES, bids)]
    super_uses = []
    for name, super_spec in (super_bids or {}).items():
        value, hit = super_spec if isinstance(super_spec, tuple) else (super_spec, False)
        records.append(BidRecord(player=name, raw_bid=value, bid=value, is_super=True))
        super_uses.append({"player": name, "value": value, "hit": hit})
    return DayResult(
        day=day,
        bids=records,
        pool=pool,
        winner=winner,
        winning_bid=winning_bid,
        auction_score=auction_score,
        super_uses=super_uses,
        collision_values=list(collision_values),
        is_void=is_void,
    )


def make_log(match_index=1, is_playoff=False, days=None, auction_scores=None, game=TEST_GAME, *, scores=None):
    return MatchLog(
        match_index=match_index,
        is_playoff=is_playoff,
        game=game,
        days=days if days is not None else [make_day(1, [5, 4, 3, 2, 1, 0], 1, "甲", 5, [], 1)],
        auction_scores=auction_scores if auction_scores is not None else {name: 0 for name in NAMES},
        scores=scores if scores is not None else {name: 0 for name in (auction_scores if auction_scores is not None else NAMES)},
    )


def make_standings(auction_scores_history: dict[str, list[int]], *, scores_history=None, total_scores=None):
    """通过生产路径排名；默认总分只含拍卖分，调用方可显式加入命中分。

    显式 scores_history 为已完成局分数；total_scores 与该历史的差额作为
    当前局分数传给引擎，确保排序和名次也使用指定总分。
    """
    if scores_history is None:
        scores_history = {name: list(scores) for name, scores in auction_scores_history.items()}
    current_scores = None if total_scores is None else {
        name: total_scores[name] - sum(scores_history[name]) for name in auction_scores_history
    }
    return compute_standings(
        auction_scores_history, scores_history=scores_history, current_match_scores=current_scores,
    )


def history(**overrides):
    """一日公布（DayResult.public_view 形状）：默认「乙 以 5 获胜、无撞车、
    无人用超级」，用 overrides 覆盖字段。调用方自行包列表作 today_history。"""
    base = {"day": 1, "winner": "乙", "winning_bid": 5, "pool": 1, "auction_score": 1,
            "bids_detail": [
                {"player": name, "bid": bid, "super_bid": None}
                for name, bid in zip(NAMES, [6, 5, 4, 3, 2, 1])
            ],
            "super_uses": [], "collision_values": []}
    base.update(overrides)
    return base


def make_obs(my_name="甲", day=1, today_history=None, total_auction_score=0, pool=1, coins=7,
             my_last_bid=None, my_last_deduction=None,
             my_last_collided=False, my_last_sniped=False, my_last_bid_illegal=False,
             my_last_super_bid=None, my_last_super_hit=False,
             all_coins=None,
             past_match_logs=None, game=TEST_GAME, *, match_score=0, total_score=0):
    """全字段 Observation 工厂。past_match_logs 默认空（即当前为第 1 局）。"""
    return Observation(
        my_name=my_name,
        my_coins=coins,
        my_match_auction_score=0,
        my_total_auction_score=total_auction_score,
        my_match_score=match_score,
        my_total_score=total_score,
        day=day,
        today_pool=pool,
        today_history=today_history or [],
        standings=[],
        past_match_logs=past_match_logs or [],
        game=game,
        my_last_bid=my_last_bid,
        my_last_deduction=my_last_deduction,
        my_last_collided=my_last_collided,
        my_last_sniped=my_last_sniped,
        my_last_bid_illegal=my_last_bid_illegal,
        my_last_super_bid=my_last_super_bid,
        my_last_super_hit=my_last_super_hit,
        all_coins=all_coins,
    )


# ---------- 2. 引擎场景构造器 ----------

def run_one_match(strategies, match_index=1, is_playoff=False, super_strategies=None,
                  game=TEST_GAME):
    """用给定的 {名字: 策略函数} 跑一局，返回 MatchLog。super_strategies 可选。"""
    super_strategies = super_strategies or {}
    players = [
        ScriptedPlayer(name, strategies[name], super_strategies.get(name)) for name in NAMES
    ]
    auction_scores_history = {name: [] for name in NAMES}
    return run_match(
        players, match_index, is_playoff, auction_scores_history, [], game,
        scores_history={name: [] for name in NAMES},
    )


def constant_by_name(bids_by_name: dict):
    """每人每天固定出价：{名字: 出价}（可表达非法原始值，如 7 / -1 / 2.5）。"""
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


# ---------- 3. AI 玩家测试夹具 ----------

ALL_NAMES = ["Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot"]
TEST_PREDICTIONS = [(name, 10) for name in ALL_NAMES]


def fake_task_runner(script):
    """脚本只模拟模型输出；严格 schema、规则、纠错均使用生产路径。"""
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

    runner = TaskRunner(SimpleNamespace(open=open_model, settings={}))
    runner.calls = calls
    return runner


def text_reply(data):
    """将测试字段写成模型文本；保留非法字段供生产解析器检查。"""
    if isinstance(data, str):
        return data
    if "memory" in data:
        return data["memory"]
    strategy = data.get("strategy", "")
    if "speech" in data:
        return f"<strategy>\n{strategy}\n</strategy>\n<speech>\n{data['speech']}\n</speech>"
    decision = {key: value for key, value in data.items() if key != "strategy"}
    return (f"<strategy>\n{strategy}\n</strategy>\n<decision>\n"
            f"{json.dumps(decision, ensure_ascii=False)}\n</decision>")


def make_player(task_runner, tmp_path=None, game=TEST_GAME):
    log_path = (tmp_path / "ai_Alpha.jsonl") if tmp_path else None
    return AIPlayer("Alpha", "测试人设", "测试人设正文", TEST_PREDICTIONS, ALL_NAMES,
                    task_runner, game=game, log_path=log_path,
                    readable_path=log_path.with_suffix(".md") if log_path else None,
                    capture_path=tmp_path / "capture.jsonl" if tmp_path else None)


def bid_reply(bid, super_bid=None, strategy="我的私有分析"):
    return {"strategy": strategy, "bid": bid, "super_bid": super_bid}


def speech_reply(speech):
    return {"strategy": "先放个烟雾弹（私有）", "speech": speech}


def make_ai_obs(day=1, coins=6, game=TEST_GAME):
    """AI 玩家测试的极简 Observation（Alpha 视角；解析测试只关心出价合法性）。"""
    return make_obs(my_name="Alpha", day=day, coins=coins, game=game)
