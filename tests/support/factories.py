"""测试工厂：数据对象、引擎场景和 AI 玩家；pytest 夹具在 tests/conftest.py。"""

from copy import deepcopy
from contextlib import asynccontextmanager
from types import SimpleNamespace
import json

from pydantic_ai.models.function import FunctionModel
from auction_game.agent.tasks import BidOutput, MemoryOutput, SpeechOutput, SummaryOutput, TaskRunner

from auction_game.agent import prompts
from auction_game.agent.log import AgentLog
from auction_game.agent.player import AIPlayer
from auction_game.agent.text_output import BOARD, LetterNames, TeamBoard
from auction_game.flow.controller import Controller
from auction_game.reporting.run_output import initialize_run
from auction_game.game.engine import MatchState, compute_standings, settle_day
from auction_game.game.lineup import Lineup, PlayerProfile
from auction_game.game.settings import GameSettings
from auction_game.game.models import DayResult, MatchLog, Observation, PlayerDayResult

NAMES = ["甲", "乙", "丙", "丁", "戊", "己"]
# NAMES 分成两个阵营：前三人红队，后三人蓝队。
TEAM_OF = {name: "红队" if index < 3 else "蓝队" for index, name in enumerate(NAMES)}
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

def make_day(day, bids, pool=1, super_bids=None, game=TEST_GAME, team_of=None):
    """用引擎结算一天。bids 是按 NAMES 顺序的正常出价，人数可少于六人；
    super_bids 是 {玩家: 超级出价}，team_of 是玩家到阵营。获胜、撞车和命中都由引擎判定。"""
    super_bids = super_bids or {}
    return settle_day(day, pool, [(name, bid, super_bids.get(name)) for name, bid in zip(NAMES, bids)], game,
                      team_of)


def make_log(match_index=1, is_playoff=False, days=None, game=TEST_GAME):
    """局日志；分数和余额由天的结算算出，默认一天、甲以 5 获胜。"""
    return MatchLog(
        match_index=match_index,
        is_playoff=is_playoff,
        game=game,
        days=days if days is not None else [make_day(1, [5, 4, 3, 2, 1, 0], game=game)],
    )


def make_logs(count, game=TEST_GAME):
    """已结束的前 count 局，都是正赛局。"""
    return [make_log(match_index=index, game=game) for index in range(1, count + 1)]


def scored_log(match_index, auction_scores, scores=None, game=TEST_GAME, *, is_playoff=False):
    """分数给定的局日志，只用于排名：有拍卖分的玩家各赢一天，奖池等于他的拍卖分；
    最后一天无人获胜，补上总分减拍卖分的命中分。scores 默认等于拍卖分。"""
    scores = scores if scores is not None else auction_scores
    names = list(auction_scores)

    def day(number, pool, score_of, winner=None):
        return DayResult(day=number, pool=pool, players=tuple(PlayerDayResult(
            player=name, bid=0, super_bid=None, outcome="won" if name == winner else "refunded",
            sniped=False, super_hit=False, coins_delta=0, score=score_of(name)) for name in names))

    days = [day(number, auction_scores[winner], lambda name, winner=winner: auction_scores[name] * (name == winner),
                winner)
            for number, winner in enumerate((name for name in names if auction_scores[name]), 1)]
    days.append(day(len(days) + 1, 1, lambda name: scores[name] - auction_scores[name]))
    return MatchLog(match_index=match_index, is_playoff=is_playoff, game=game, days=days)


def make_standings(auction_scores_history: dict[str, list[int]], *, scores_history=None, team_of=None):
    """通过生产路径排名；每名玩家的历史一样长，第 i 项是第 i 局。不给 scores_history 时总分只含拍卖分。
    team_of 是玩家到阵营，给了就是团队赛的排名。"""
    if scores_history is None:
        scores_history = auction_scores_history
    names = list(auction_scores_history)
    match_count = len(next(iter(auction_scores_history.values())))
    logs = [scored_log(index + 1, {name: auction_scores_history[name][index] for name in names},
                       {name: scores_history[name][index] for name in names})
            for index in range(match_count)]
    return compute_standings(names, logs, team_of=team_of)


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
        my_match_score=match_score,
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

def run_one_match(strategies, match_index=1, is_playoff=False, super_strategies=None, game=TEST_GAME, *,
                  names=NAMES, finished_logs=(), team_of=None):
    """用 {名字: 策略函数} 跑一局，返回 MatchLog。strategy(obs) 返回正常出价；super_strategies 可选，
    其中的函数返回超级出价或 None。每天按座位顺序先调正常出价策略，再调超级出价策略。"""
    super_strategies = super_strategies or {}
    match = MatchState(names, match_index, is_playoff, list(finished_logs), game, team_of=team_of)
    for _ in range(game.max_days):
        observations = match.start_day()
        submissions = {}
        for name, obs in observations.items():
            bid = strategies[name](obs)
            submissions[name] = bid, super_strategies[name](obs) if name in super_strategies else None
        match.settle(submissions)
    return match.log


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


def run_scenario(table, default=0, super_strategies=None, capture=None, game=TEST_GAME, team_of=None):
    """最常见场景：全员共用一张按天出价表（表内按名字区分出价），跑一局返回 MatchLog。"""
    strategy = bids_by_day(table, default=default, capture=capture)
    return run_one_match(
        {name: strategy for name in NAMES}, super_strategies=super_strategies, game=game, team_of=team_of
    )


# ---------- 整场流程：不调模型的玩家按出价表行动 ----------

class StubPlayer:
    """供 AIDirector.run() 跑整场的玩家，不调模型。table[(局, 天)][名字] 是 (正常出价, 超级出价)，
    没列出的出 (0, None)；每局的规划交出第 1 天的出价。titles 按顺序记下每个任务所在那一轮的标题，
    observations 记下出价时看到的信息。"""

    def __init__(self, name, table=None):
        self.name = name
        self.table = table or {}
        self.titles = []
        self.observations = []
        self._plans = 0

    def checkpoint(self):
        return None

    def rollback(self, checkpoint):
        pass

    def _submission(self, match_index, day):
        bid, super_bid = self.table.get((match_index, day), {}).get(self.name, (0, None))
        return BidOutput("策略", bid, super_bid, [])

    def give_speech(self, text, title, *, letters=True):
        self.titles.append(title)
        return SpeechOutput("策略", f"{self.name}的发言", [])

    def plan_strategy(self, text, title):
        self.titles.append(title)
        self._plans += 1
        return self._submission(self._plans, 1)

    def save_memory(self, text, title):
        self.titles.append(title)
        return MemoryOutput("记忆")

    def bid(self, obs, received, title):
        self.titles.append(title)
        self.observations.append(obs)
        return self._submission(obs.match_index, obs.day)

    def final_summary(self, text, title):
        self.titles.append(title)
        return SummaryOutput(f"{self.name}的总结")


def init_run_dir(tmp_path, names):
    """初始化一个离线场次目录，整场流程往里写发言、局日志和 README。"""
    run_dir = tmp_path / "run"
    initialize_run(run_dir, {"run_type": "offline", "mode": "regular",
                             "players": [{"name": name} for name in names]}, "测试规则")
    return run_dir


# ---------- AI 玩家：只替换模型输出，解析、校验和纠错走生产 Agent ----------

def make_lineup(names=NAMES, rates=None, persona=("人设", "正文")):
    """按座位的阵容，全员人设相同；rates 是各座位的预测胜率，默认每人 10。"""
    rates = rates or [10] * len(names)
    return Lineup(tuple(PlayerProfile(name, *persona, rate) for name, rate in zip(names, rates, strict=True)))


def make_team_lineup(team_of=TEAM_OF, persona=("人设", "正文")):
    """团队赛阵容：team_of 是按座位的玩家到阵营，全员人设相同，没有预测胜率。"""
    return Lineup(tuple(PlayerProfile(name, *persona, None, team) for name, team in team_of.items()))


ALL_NAMES = ["Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot"]
TEST_LINEUP = make_lineup(ALL_NAMES, persona=("测试人设", "测试人设正文"))


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
    """将测试字段写成模型文本；保留非法字段供生产解析器检查。letters 写成信的块，不进 decision。"""
    if isinstance(data, str):
        return data
    if "memory" in data:
        return data["memory"]
    strategy = data.get("strategy", "")
    letters = "".join(f"\n<{letter['to']}>\n{letter['text']}\n</{letter['to']}>"
                      for letter in data.get("letters", []))
    if "speech" in data:
        return f"<strategy>\n{strategy}\n</strategy>\n<speech>\n{data['speech']}\n</speech>{letters}"
    if "letter" in data:
        return f"<strategy>\n{strategy}\n</strategy>\n<{BOARD}>\n{data['letter']}\n</{BOARD}>"
    decision = {key: value for key, value in data.items() if key not in ("strategy", "letters")}
    return (f"<strategy>\n{strategy}\n</strategy>\n<decision>\n"
            f"{json.dumps(decision, ensure_ascii=False)}\n</decision>{letters}")


def make_player(task_runner, tmp_path, game=TEST_GAME, name="Alpha"):
    """阵容是 TEST_LINEUP，name 要在 ALL_NAMES 中。"""
    profile = next(profile for profile in TEST_LINEUP.profiles if profile.name == name)
    return AIPlayer(name, prompts.build_system_prompt(profile, TEST_LINEUP, game),
                    LetterNames(name, TEST_LINEUP.others(name)), task_runner,
                    AgentLog(name, tmp_path / f"ai_{name}.jsonl", readable_path=tmp_path / f"ai_{name}.md"),
                    game=game)


def make_team_player(task_runner, tmp_path, game=TEST_GAME, name="甲"):
    """阵容是 make_team_lineup()，name 要在 TEAM_OF 中。"""
    lineup = make_team_lineup()
    profile = next(profile for profile in lineup.profiles if profile.name == name)
    return AIPlayer(name, prompts.build_system_prompt(profile, lineup, game), TeamBoard(), task_runner,
                    AgentLog(name, tmp_path / f"ai_{name}.jsonl", readable_path=tmp_path / f"ai_{name}.md"),
                    game=game)


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


def discussion_reply(letter):
    return {"strategy": "讨论的私有考虑", "letter": letter}


def make_ai_obs(day=2, coins=6, game=TEST_GAME, name="Alpha"):
    """ALL_NAMES 中 name 视角第 day 天的出价依据：之前每天全员出 0 流局，全员余额都是 coins。

    出价请求从第 2 天起发出，所以默认第 2 天。
    """
    history = [settle_day(number, game.daily_pool[number - 1], [(name, 0, None) for name in ALL_NAMES], game)
               for number in range(1, day)]
    return Observation(
        my_name=name, my_coins=coins, my_match_score=0, match_index=1, day=day,
        today_pool=game.daily_pool[day - 1], today_history=history,
        standings=compute_standings(ALL_NAMES, []), game=game,
        all_coins={name: coins for name in ALL_NAMES},
    )
