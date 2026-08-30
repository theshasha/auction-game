"""测试共享工厂与场景构造器（pytest 自动加载；各测试文件 from conftest import ...）。

分三节：
1. 通用数据对象工厂：NAMES / make_day / make_log / make_standings / history / make_obs
   （v5 结构：DayResult 用 super_uses + collision_values；Observation 含 my_last_sniped
   与 my_last_super_hit）。
2. 引擎场景构造器：run_one_match / constant_strategies / constant_by_name /
   bids_by_day / super_by_day / run_scenario——消灭「按天查表策略」在各用例里
   逐字重写的样板。策略只依赖 obs.day 与 obs.my_name，可全员共用同一个函数。
3. AI 玩家测试夹具：ALL_NAMES / TEST_PREDICTIONS / fake_chat_fn / make_player /
   bid_reply / speech_reply / legacy_speech_reply / make_ai_obs。
"""

from ai_player import AIPlayer
from engine import run_match
from models import BidRecord, DayResult, MatchLog, Observation, StandingsEntry
from players import ScriptedPlayer

NAMES = ["甲", "乙", "丙", "丁", "戊", "己"]


# ---------- 1. 通用数据对象工厂 ----------

def make_day(day, bids, pool, winner, winning_bid, collision_values, wins_taken=0,
             is_void=False, super_bids=None):
    """构造一天结果。bids 为六人正常出价；super_bids 为 {玩家: 数值} 或
    {玩家: (数值, 是否命中)}，每个使用者在出价表里追加一笔 is_super=True
    的记录，并在 super_uses 里登记 {"player", "value", "hit"}（v5 起每日全透明公布）。
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
        wins_taken=wins_taken,
        super_uses=super_uses,
        collision_values=list(collision_values),
        is_void=is_void,
    )


def make_log(match_index=1, is_playoff=False, days=None, wins=None):
    return MatchLog(
        match_index=match_index,
        is_playoff=is_playoff,
        days=days if days is not None else [make_day(1, [5, 4, 3, 2, 1, 0], 1, "甲", 5, [], 1)],
        wins=wins if wins is not None else {name: 0 for name in NAMES},
    )


def make_standings(wins_history: dict[str, list[int]]):
    entries = [
        StandingsEntry(player=name, total_wins=sum(per_match), wins_per_match=list(per_match))
        for name, per_match in wins_history.items()
    ]
    entries.sort(key=lambda entry: (-entry.total_wins, entry.player))
    for index, entry in enumerate(entries):
        entry.rank = entries[index - 1].rank if index > 0 and entry.total_wins == entries[index - 1].total_wins else index + 1
    return entries


def history(**overrides):
    """一日公布（DayResult.public_view 形状）：默认「乙 以 5 获胜、无撞车、无人用超级」，
    用 overrides 覆盖字段。调用方自行包列表作 today_history。"""
    base = {"day": 1, "winner": "乙", "winning_bid": 5, "pool": 1, "wins_taken": 1,
            "super_uses": [], "collision_values": []}
    base.update(overrides)
    return base


def make_obs(my_name="甲", day=1, today_history=None, total_wins=0, pool=1, coins=7,
             my_last_bid=None, my_last_deduction=None,
             my_last_collided=False, my_last_sniped=False, my_last_bid_illegal=False,
             my_super_available=True, my_super_used_day=None,
             my_last_super_bid=None, my_last_super_hit=False,
             others_coins_total=None, others_super_remaining=None,
             past_match_logs=None):
    """全字段 Observation 工厂（v5）。past_match_logs 默认空（即当前为第 1 局）。"""
    return Observation(
        my_name=my_name,
        my_coins=coins,
        my_match_wins=0,
        my_total_wins=total_wins,
        day=day,
        today_pool=pool,
        today_history=today_history or [],
        standings=[],
        past_match_logs=past_match_logs or [],
        my_super_available=my_super_available,
        my_super_used_day=my_super_used_day,
        my_last_bid=my_last_bid,
        my_last_deduction=my_last_deduction,
        my_last_collided=my_last_collided,
        my_last_sniped=my_last_sniped,
        my_last_bid_illegal=my_last_bid_illegal,
        my_last_super_bid=my_last_super_bid,
        my_last_super_hit=my_last_super_hit,
        others_coins_total=others_coins_total,
        others_super_remaining=others_super_remaining,
    )


# ---------- 2. 引擎场景构造器 ----------

def run_one_match(strategies, match_index=1, is_playoff=False, super_strategies=None):
    """用给定的 {名字: 策略函数} 跑一局，返回 MatchLog。super_strategies 可选。"""
    super_strategies = super_strategies or {}
    players = [
        ScriptedPlayer(name, strategies[name], super_strategies.get(name)) for name in NAMES
    ]
    wins_history = {name: [] for name in NAMES}
    return run_match(players, match_index, is_playoff, wins_history, [])


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


def run_scenario(table, default=0, super_strategies=None, capture=None):
    """最常见场景：全员共用一张按天出价表（表内按名字区分出价），跑一局返回 MatchLog。"""
    strategy = bids_by_day(table, default=default, capture=capture)
    return run_one_match({name: strategy for name in NAMES}, super_strategies=super_strategies)


# ---------- 3. AI 玩家测试夹具 ----------

ALL_NAMES = ["Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot"]
TEST_PREDICTIONS = [(name, 10) for name in ALL_NAMES]


def fake_chat_fn(script):
    """script: 回复文本或异常的有序列表，每次调用弹出下一个。"""
    queue = list(script)
    calls = []

    def chat_fn(messages):
        calls.append(messages)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    chat_fn.calls = calls
    return chat_fn


def make_player(chat_fn, tmp_path=None):
    log_path = (tmp_path / "ai_Alpha.jsonl") if tmp_path else None
    return AIPlayer("Alpha", "测试人设", "测试人设正文", TEST_PREDICTIONS, ALL_NAMES, chat_fn, log_path=log_path)


def bid_reply(bid):
    return f"我的分析：……\n```json\n{{\"bid\": {bid}}}\n```"


def speech_reply(speech):
    """公开发言回复（当前格式）：先规划策略，末尾定界符块给出实际公开发言。"""
    return f"发言策略：先放个烟雾弹。\n【公开发言】\n{speech}\n【发言结束】"


def legacy_speech_reply(speech):
    """旧格式发言回复：末尾裸 JSON {"speech": ...}（解析器保留兼容）。"""
    return f'发言策略：先放个烟雾弹。\n{{"speech": "{speech}"}}'


def make_ai_obs(day=1, coins=6, super_available=True):
    """AI 玩家测试的极简 Observation（Alpha 视角；解析测试只关心出价合法性）。"""
    return make_obs(my_name="Alpha", day=day, coins=coins, my_super_available=super_available)
