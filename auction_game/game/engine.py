"""单局规则：判定和结算一天的出价、累计排名，以及进行中一局的状态。"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Optional

from auction_game.game.settings import GameSettings, validate_submission
from auction_game.game.models import (
    DayResult,
    MatchLog,
    Observation,
    PlayerDayResult,
    StandingsEntry,
    TeamStandingsEntry,
)


Submission = tuple[str, int, Optional[int]]  # (玩家, 正常出价, 超级出价或 None)


def resolve_bids(
    normal_bids: Iterable[tuple[str, int]], super_bids: Iterable[tuple[str, int]],
    team_of: dict[str, str] | None = None,
) -> tuple[Optional[str], set[int], set[str], set[str]]:
    """判定当天结果，返回获胜者、撞车价位、正常出价被命中的玩家和超级出价命中的玩家；只有正常出价能获胜。

    super_bids 是 (玩家, 狙击的价位)。超级出价只命中其他阵营的正常出价：价位上有其他阵营的正常出价，
    这笔超级出价就命中，那些出价撞车。同价位上狙击者队友的出价不算被命中，但和对方同价，照样撞车。
    team_of 是玩家到阵营，None 时每人自成一个阵营：规则禁止超级出价和自己的正常出价相同，
    所以和"价位上有正常出价就命中，这个价位的出价都被命中"的判定相同。
    """
    normal_bids = list(normal_bids)
    side = (lambda player: player) if team_of is None else team_of.__getitem__
    bidders_at: dict[int, list[str]] = {}
    for player, bid in normal_bids:
        bidders_at.setdefault(bid, []).append(player)
    sniped, hitters = set(), set()
    for shooter, value in super_bids:
        targets = [player for player in bidders_at.get(value, []) if side(player) != side(shooter)]
        if targets:
            sniped.update(targets)
            hitters.add(shooter)
    burned_values = {value for value, bidders in bidders_at.items() if len(bidders) >= 2}
    burned_values |= {bid for player, bid in normal_bids if player in sniped}
    winner = max(
        ((player, bid) for player, bid in normal_bids if bid not in burned_values),
        key=lambda item: item[1],
        default=(None, None),
    )[0]
    return winner, burned_values, sniped, hitters


def settle_day(day: int, pool: int, submissions: list[Submission], game: GameSettings,
               team_of: dict[str, str] | None = None) -> DayResult:
    """按规则结算一天的出价，出价须已通过校验；命中按 team_of 的阵营判定，见 resolve_bids。

    获胜和撞车的正常出价全额扣除，其余退还；超级出价每次扣固定成本，命中各领一份悬赏和命中分。
    获胜者取得当天奖池的拍卖分，流局时奖池作废。
    """
    winner, burned_values, sniped, hitters = resolve_bids(
        ((player, bid) for player, bid, _ in submissions),
        ((player, super_bid) for player, _, super_bid in submissions if super_bid is not None),
        team_of,
    )
    results = []
    for player, bid, super_bid in submissions:
        if player == winner:
            outcome = "won"
        elif bid in burned_values:
            outcome = "collided"
        else:
            outcome = "refunded"
        super_hit = player in hitters
        coins_delta = 0 if outcome == "refunded" else -bid
        score = pool if outcome == "won" else 0
        if super_bid is not None:
            coins_delta -= game.super_cost
        if super_hit:
            coins_delta += game.super_bounty
            score += game.super_hit_score
        results.append(PlayerDayResult(
            player=player, bid=bid, super_bid=super_bid, outcome=outcome, sniped=player in sniped,
            super_hit=super_hit, coins_delta=coins_delta, score=score,
        ))
    return DayResult(day=day, pool=pool, players=tuple(results))


def compute_standings(names: list[str], logs: list[MatchLog], current: MatchLog | None = None,
                      team_of: dict[str, str] | None = None) -> list[StandingsEntry]:
    """按累计总分排名，同分并列；logs 是已结束的局，进行中的 current 只计入累计值。

    各局明细只包含已结束的局，拍卖分单独保留，不用于破同分。团队赛时每条带上 team_of 里的阵营，
    阵营排名由 team_standings 从这份排名汇总。
    """
    current_scores = current.scores if current is not None else {}
    current_auction_scores = current.auction_scores if current is not None else {}
    entries = [
        StandingsEntry(
            player=name,
            total_score=sum(log.scores[name] for log in logs) + current_scores.get(name, 0),
            total_auction_score=sum(log.auction_scores[name] for log in logs) + current_auction_scores.get(name, 0),
            scores_per_match=[log.scores[name] for log in logs],
            team=team_of[name] if team_of is not None else None,
        )
        for name in names
    ]
    entries.sort(key=lambda entry: (-entry.total_score, entry.player))
    _assign_ranks(entries)
    return entries


def team_standings(standings: list[StandingsEntry]) -> list[TeamStandingsEntry] | None:
    """把个人排名按阵营汇总成阵营排名，同分并列、按阵营名排；个人赛的排名没有阵营，返回 None。"""
    if standings[0].team is None:
        return None
    teams: dict[str, TeamStandingsEntry] = {}
    for entry in standings:
        team = teams.setdefault(entry.team, TeamStandingsEntry(entry.team, 0, 0, [0] * len(entry.scores_per_match)))
        team.total_score += entry.total_score
        team.total_auction_score += entry.total_auction_score
        team.scores_per_match = [total + score for total, score in zip(team.scores_per_match, entry.scores_per_match)]
    entries = sorted(teams.values(), key=lambda entry: (-entry.total_score, entry.team))
    _assign_ranks(entries)
    return entries


def _assign_ranks(entries: list[StandingsEntry] | list[TeamStandingsEntry]) -> None:
    """entries 已按总分降序排好；同分的名次相同，下一名跳过并列的人数。"""
    for index, entry in enumerate(entries):
        if index > 0 and entry.total_score == entries[index - 1].total_score:
            entry.rank = entries[index - 1].rank
        else:
            entry.rank = index + 1


class MatchState:
    """进行中的一局：每名玩家的金币和已结算的天，按 start_day、settle 交替推进。"""

    def __init__(self, names: list[str], match_index: int, is_playoff: bool,
                 finished_logs: list[MatchLog], game: GameSettings, *, team_of: dict[str, str] | None = None):
        """finished_logs 是已结束的各局，只用于累计排名；team_of 是玩家到阵营，个人赛为 None。"""
        self.names = list(names)
        self.finished_logs = list(finished_logs)
        self.game = game
        self.team_of = team_of
        self.coins = {name: game.initial_coins for name in names}
        self.log = MatchLog(match_index=match_index, is_playoff=is_playoff, game=game)

    def standings(self) -> list[StandingsEntry]:
        """含本局已结算部分的累计排名。"""
        return compute_standings(self.names, self.finished_logs, self.log, self.team_of)

    def start_day(self) -> dict[str, Observation]:
        """开始下一天，第 2 天起先发工资；返回每名玩家当天能看到的信息，按座位顺序。

        全员共用同一份排名、余额和本局记录。
        """
        day = len(self.log.days) + 1
        if day >= 2:
            for name in self.coins:
                self.coins[name] += self.game.daily_salary
        # 全员余额第 2 天起公布，取发工资后的值；复制一份，当天结算不会改动它。
        all_coins = dict(self.coins) if day >= 2 else None
        standings = self.standings()
        history = list(self.log.days)
        match_scores = self.log.scores
        return {name: Observation(
            my_name=name,
            my_coins=self.coins[name],
            my_match_score=match_scores.get(name, 0),
            match_index=self.log.match_index,
            day=day,
            today_pool=self.game.daily_pool[day - 1],
            today_history=history,
            standings=standings,
            game=self.game,
            all_coins=all_coins,
        ) for name in self.names}

    def settle(self, submissions: dict[str, tuple[int, Optional[int]]]) -> DayResult:
        """结算 start_day 开始的这一天：submissions 是玩家到 (正常出价, 超级出价)。

        出价不符合规则时抛出 ValueError：AI 的违规在代理层纠错，到这里就是程序错误。
        """
        day = len(self.log.days) + 1
        entries = [(name, *submissions[name]) for name in self.names]
        for name, bid, super_bid in entries:
            reason = validate_submission(bid, super_bid, self.coins[name], self.game)
            if reason is not None:
                raise ValueError(f"{name} 的出价不符合规则：{reason}")
        result = settle_day(day, self.game.daily_pool[day - 1], entries, self.game, self.team_of)
        for player_result in result.players:
            self.coins[player_result.player] += player_result.coins_delta
        self.log.days.append(result)
        return result
