"""团队赛两队对比的指标，口径见 docs/analyze.md 的"团队赛对比"；打印和命令行入口在 auction_game.cli.team_report。

余额日志里没有，按引擎的顺序重算：每局 initial_coins 起，第 2 天起每天开始先发 daily_salary，当天结束加 coins_delta。
当天重判都用引擎的 resolve_bids，带阵营。比赛参数一律取局日志里的 game。
"""

from __future__ import annotations

import random
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from auction_game.game.engine import resolve_bids
from auction_game.game.settings import GameSettings
from auction_game.reporting.analyze import collect_team_stats, load_match_logs
from auction_game.reporting.run_paths import RunPaths

# 分析用的分档，不是比赛参数。
HIGH_BID_FLOOR = 5          # "出价至少 5"的人天
LOW_BALANCE_CEILING = 9     # "余额不超过 9"的人天
TOP_BID_LEVELS = 3          # 最高的几档正常出价，现行参数下是 10–12
BOOTSTRAP_ROUNDS = 20000
BOOTSTRAP_SEED = 0


@dataclass(frozen=True)
class Ratio:
    """part/whole，多场合计时分子分母各自相加。"""

    part: int
    whole: int

    def __add__(self, other: Ratio) -> Ratio:
        return Ratio(self.part + other.part, self.whole + other.whole)


@dataclass(frozen=True)
class Mean:
    """total ÷ count，多场合计时分子分母各自相加；count 为 0 时没有均值。"""

    total: int
    count: int

    def __add__(self, other: Mean) -> Mean:
        return Mean(self.total + other.total, self.count + other.count)

    @property
    def value(self) -> float | None:
        return self.total / self.count if self.count else None


@dataclass(frozen=True)
class TeamRun:
    name: str
    teams: tuple[str, str]   # 按场次快照里玩家的顺序
    team_of: dict[str, str]
    matches: list[dict]


def load_team_run(run_dir: Path) -> TeamRun:
    """读场次快照里的阵营和已结束的局；不是团队赛时报 ValueError。"""
    players = RunPaths(run_dir).read_config()["players"]
    if not all(pc["team"] for pc in players):
        raise ValueError(f"{run_dir} 不是团队赛场次")
    team_of = {pc["name"]: pc["team"] for pc in players}
    return TeamRun(run_dir.name, tuple(dict.fromkeys(team_of.values())), team_of, load_match_logs(run_dir))


def _days(matches: list[dict]):
    """按顺序给出每一天：(局序号, 比赛参数, 当天日志, 出价前余额, 同一局前一天的日志或 None)。"""
    for payload in matches:
        match = payload["match"]
        game = GameSettings.from_dict(match["game"])
        coins = {result["player"]: game.initial_coins for result in match["days"][0]["players"]}
        previous = None
        for day in match["days"]:
            if day["day"] >= 2:
                coins = {name: value + game.daily_salary for name, value in coins.items()}
            yield match["match_index"], game, day, coins, previous
            coins = {result["player"]: coins[result["player"]] + result["coins_delta"] for result in day["players"]}
            previous = day


def bid_caps(balance: int, game: GameSettings) -> tuple[int, int | None]:
    """当天正常出价的上限：不带狙时，带狙时（余额不够开枪时为 None）。"""
    with_super = min(balance - game.super_cost, game.max_bid) if balance >= game.super_cost else None
    return min(balance, game.max_bid), with_super


def balance_prices(balance: int, game: GameSettings) -> set[int]:
    """余额价：一个人当天正常出价能出到的上限，带狙和不带狙两种。"""
    return {cap for cap in bid_caps(balance, game) if cap is not None}


def _winner_team(results: list[dict], team_of: dict[str, str], *, supers_of=None, dropped=()) -> str | None:
    """重判当天，返回获胜阵营，流局为 None。supers_of 为保留超级出价的阵营集合（None 为全部保留），
    dropped 里的玩家当作没有正常出价。"""
    winner = resolve_bids(
        ((result["player"], result["bid"]) for result in results if result["player"] not in dropped),
        ((result["player"], result["super_bid"]) for result in results
         if result["super_bid"] is not None and (supers_of is None or team_of[result["player"]] in supers_of)),
        team_of,
    )[0]
    return team_of[winner] if winner is not None else None


def bootstrap_interval(samples: list[int], *, rounds: int = BOOTSTRAP_ROUNDS,
                       seed: int = BOOTSTRAP_SEED) -> tuple[int, int]:
    """有放回地抽同样多的样本求和，重复 rounds 次，返回 2.5% 和 97.5% 分位。"""
    rng = random.Random(seed)
    sums = sorted(sum(rng.choices(samples, k=len(samples))) for _ in range(rounds))
    return sums[int(rounds * 0.025)], sums[int(rounds * 0.975)]


def collect_team_metrics(matches: list[dict], team_of: dict[str, str]) -> dict:
    """一场团队赛的两队指标。成对的值是 (前队, 后队) 的元组，阵营按 team_of 的顺序；合并两队的是单个值。"""
    teams = tuple(dict.fromkeys(team_of.values()))
    other = {teams[0]: teams[1], teams[1]: teams[0]}
    team_size = {team: list(team_of.values()).count(team) for team in teams}
    c = {team: Counter() for team in teams}
    by_day_index: dict[int, dict[str, Counter]] = {}
    day_diffs: list[int] = []
    best_fixed = {team: Counter() for team in teams}
    keep_one_days: dict[str, list[tuple[int, int, int]]] = {team: [] for team in teams}
    first_day_tables, void_tables = [], []
    first_day_wins = Counter()
    total_days = 0

    for match_index, game, day, coins, previous in _days(matches):
        total_days += 1
        results = day["players"]
        pool, number = day["pool"], day["day"]
        actual = team_of[day["winner"]] if day["winner"] is not None else None
        day_scores = Counter()
        counts = by_day_index.setdefault(number, {team: Counter() for team in teams})
        bids_of = {team: sorted(r["bid"] for r in results if team_of[r["player"]] == team) for team in teams}
        bidders_at: dict[int, list[str]] = {}
        for result in results:
            bidders_at.setdefault(result["bid"], []).append(result["player"])

        if number == 1:
            first_day_tables.append((match_index, *(bids_of[team] for team in teams),
                                     len(set(bids_of[teams[0]]) & set(bids_of[teams[1]]))))
            if actual is not None:
                first_day_wins.update(total=day["winning_bid"], count=1)
        if actual is None:
            void_tables.append((match_index, number, *(bids_of[team] for team in teams)))

        # 去掉超级出价只重算当天，正常出价不变。
        without = {"all": _winner_team(results, team_of, supers_of=()),
                   "first": _winner_team(results, team_of, supers_of={teams[1]}),
                   "second": _winner_team(results, team_of, supers_of={teams[0]})}
        for key, winner in without.items():
            if winner is not None:
                c[winner][f"auction_without_{key}"] += pool
        # 去掉全部超级出价后流局的天，实际也流局：超级出价只会让更多出价作废。
        if actual is not None and without["all"] == other[actual]:
            c[actual]["switched_to_own"] += 1
            c[actual]["switched_to_own_points"] += pool
        if actual is None and without["all"] is not None:
            c[without["all"]]["win_to_void"] += 1

        for team in teams:
            own = [r for r in results if team_of[r["player"]] == team]
            opponents = [r for r in results if team_of[r["player"]] != team]
            totals = c[team]
            # 事后最佳固定狙价：每个价位上对方当天有人出的天数。
            best_fixed[team].update(set(bids_of[other[team]]))

            # 队内撞车：同一价位上本队 2 笔及以上正常出价。
            own_bid_counts = Counter(r["bid"] for r in own)
            for value, count in own_bid_counts.items():
                if count >= 2:
                    totals["intra_burn"] += value * count
            # 只留一人重算：撞车价位上只保留一人的正常出价，超级出价照旧。撞车的人出价相同，
            # 超级出价只命中对方，所以留下哪一人，当天归属的阵营都一样，只有获胜的人不同。
            if actual != team:
                for value, count in own_bid_counts.items():
                    if count < 2:
                        continue
                    colliders = [r["player"] for r in own if r["bid"] == value]
                    if _winner_team(results, team_of, dropped=set(colliders[1:])) == team:
                        keep_one_days[team].append((match_index, number, pool))
                        break

            # 超级出价：按价位分组，看集火和单独打。
            shots_at: dict[int, list[dict]] = {}
            for r in own:
                if r["super_bid"] is not None:
                    shots_at.setdefault(r["super_bid"], []).append(r)
            for value, shooters in shots_at.items():
                hit = shooters[0]["super_hit"]
                if len(shooters) >= 2:
                    totals["group_shots"] += len(shooters)
                    totals["group_hits"] += len(shooters) * hit
                else:
                    totals["single_shots"] += 1
                    totals["single_hits"] += hit
                if len(shooters) == team_size[team]:
                    totals["full_team_days"] += 1
                    totals["full_team_hit_days"] += hit

            opponent_prices = set().union(*(balance_prices(coins[r["player"]], game) for r in opponents))
            yesterday_opponent_bids = ({r["bid"] for r in previous["players"] if team_of[r["player"]] != team}
                                       if previous is not None else set())
            yesterday_own_bids = ({r["bid"] for r in previous["players"] if team_of[r["player"]] == team}
                                  if previous is not None else set())
            for r in own:
                balance, bid, value, hit = coins[r["player"]], r["bid"], r["super_bid"], r["super_hit"]
                day_scores[team] += r["score"]
                totals["score"] += r["score"]
                totals["person_days"] += 1
                counts[team]["score"] += r["score"]
                if r["outcome"] == "won":
                    totals["auction"] += pool
                    totals["days_won"] += 1
                    counts[team]["days_won"] += 1
                if r["outcome"] == "collided":
                    totals["all_burn"] += bid
                if number == 1:
                    totals["first_day_bid_total"] += bid
                    totals["first_day_bids"] += 1
                else:
                    totals["later_score"] += r["score"]
                    totals["later_person_days"] += 1
                    totals["later_balance"] += balance
                    totals["reused"] += bid in yesterday_own_bids
                if number == game.max_days:
                    totals["last_day_balance"] += balance
                    totals["last_day_person_days"] += 1

                # 正常出价是否贴当天上限，带狙和不带狙的上限不同。
                plain_cap, super_cap = bid_caps(balance, game)
                capped = bid == (super_cap if value is not None else plain_cap)
                prefix = "capped" if capped else "uncapped"
                totals[prefix] += 1
                totals[f"{prefix}_sniped"] += r["sniped"]
                if bid >= HIGH_BID_FLOOR:
                    totals[f"{prefix}_high"] += 1
                    totals[f"{prefix}_high_sniped"] += r["sniped"]
                if balance <= LOW_BALANCE_CEILING:
                    totals["low_balance"] += 1
                    totals["low_balance_capped"] += capped
                if bid > game.max_bid - TOP_BID_LEVELS:
                    totals["top_bids"] += 1
                    totals["top_bids_won"] += r["outcome"] == "won"
                    totals["top_bids_sniped"] += r["sniped"]

                if value is None:
                    continue
                totals["super_uses"] += 1
                totals["super_hits"] += hit
                totals["super_net_coins"] += hit * game.super_bounty - game.super_cost
                if number == 1:
                    totals["first_day_supers"] += 1
                else:
                    totals["later_shots"] += 1
                    if value in yesterday_opponent_bids:
                        totals["yesterday_shots"] += 1
                        totals["yesterday_hits"] += hit
                # 余额价分三类：打 max_bid、第 1 天，其余才是按余额读出的。
                kind = "max" if value == game.max_bid else "first_day" if number == 1 else "read"
                if value in opponent_prices:
                    totals["balance_shots"] += 1
                    totals["balance_hits"] += hit
                    totals[f"balance_{kind}_shots"] += 1
                    totals[f"balance_{kind}_hits"] += hit
                # 命中的目标出价是不是目标当天的余额价，不看目标开没开枪。
                if hit and any(value in balance_prices(coins[player], game) for player in bidders_at[value]
                               if team_of[player] != team):
                    totals["target_balance"] += 1
                    totals[f"target_{kind}"] += 1
        day_diffs.append(day_scores[teams[0]] - day_scores[teams[1]])

    collisions = collect_team_stats(matches, team_of)

    def pair(make):
        return tuple(make(team) for team in teams)

    def ratio(part, whole):
        return pair(lambda team: Ratio(c[team][part], c[team][whole]))

    best_fixed_prices = {}
    for team in teams:
        top = max(best_fixed[team].values(), default=0)
        best_fixed_prices[team] = (sorted(v for v, n in best_fixed[team].items() if n == top), Ratio(top, total_days))

    return {
        "teams": teams,
        "total_days": total_days,
        "match_count": len(matches),
        "playoff_count": sum(payload["match"]["is_playoff"] for payload in matches),
        # 得分
        "total_score": pair(lambda team: c[team]["score"]),
        "auction_score": pair(lambda team: c[team]["auction"]),
        "hit_score": pair(lambda team: c[team]["score"] - c[team]["auction"]),
        "days_won": pair(lambda team: c[team]["days_won"]),
        "score_by_day": {number: pair(lambda team: counts[team]["score"])
                         for number, counts in sorted(by_day_index.items())},
        "days_won_by_day": {number: pair(lambda team: counts[team]["days_won"])
                            for number, counts in sorted(by_day_index.items())},
        "later_score": pair(lambda team: c[team]["later_score"]),
        "first_day_bid": pair(lambda team: Mean(c[team]["first_day_bid_total"], c[team]["first_day_bids"])),
        "first_day_winning_bid": Mean(first_day_wins["total"], first_day_wins["count"]),
        "score_diff": (sum(day_diffs), *bootstrap_interval(day_diffs)),
        # 超级出价
        "super_uses": ratio("super_uses", "person_days"),
        "super_uses_combined": Ratio(sum(c[team]["super_uses"] for team in teams),
                                     sum(c[team]["person_days"] for team in teams)),
        "super_hits": ratio("super_hits", "super_uses"),
        "first_day_supers": pair(lambda team: c[team]["first_day_supers"]),
        "super_net_coins": pair(lambda team: c[team]["super_net_coins"]),
        "auction_without_all": pair(lambda team: c[team]["auction_without_all"]),
        "auction_without_first": pair(lambda team: c[team]["auction_without_first"]),
        "auction_without_second": pair(lambda team: c[team]["auction_without_second"]),
        "switched_to_own": pair(lambda team: c[team]["switched_to_own"]),
        "switched_to_own_points": pair(lambda team: c[team]["switched_to_own_points"]),
        "win_to_void": pair(lambda team: c[team]["win_to_void"]),
        "super_auction_gain": pair(lambda team: c[team]["auction"] - c[team]["auction_without_all"]),
        "balance_shots": ratio("balance_shots", "super_uses"),
        "balance_hits": ratio("balance_hits", "balance_shots"),
        "balance_max": ratio("balance_max_hits", "balance_max_shots"),
        "balance_first_day": ratio("balance_first_day_hits", "balance_first_day_shots"),
        "balance_read": ratio("balance_read_hits", "balance_read_shots"),
        "target_balance": ratio("target_balance", "super_hits"),
        "target_max": pair(lambda team: c[team]["target_max"]),
        "target_first_day": pair(lambda team: c[team]["target_first_day"]),
        "target_read": pair(lambda team: c[team]["target_read"]),
        "group_shots": ratio("group_hits", "group_shots"),
        "single_shots": ratio("single_hits", "single_shots"),
        "full_team_days": ratio("full_team_hit_days", "full_team_days"),
        "yesterday_shots": ratio("yesterday_shots", "later_shots"),
        "yesterday_hits": ratio("yesterday_hits", "yesterday_shots"),
        "best_fixed_price": pair(lambda team: best_fixed_prices[team]),
        # 正常出价和金币
        "capped": ratio("capped", "person_days"),
        "capped_sniped": ratio("capped_sniped", "capped"),
        "uncapped_sniped": ratio("uncapped_sniped", "uncapped"),
        "capped_high_sniped": ratio("capped_high_sniped", "capped_high"),
        "uncapped_high_sniped": ratio("uncapped_high_sniped", "uncapped_high"),
        "low_balance_capped": ratio("low_balance_capped", "low_balance"),
        "top_bids": pair(lambda team: c[team]["top_bids"]),
        "top_bids_won": ratio("top_bids_won", "top_bids"),
        "top_bids_sniped": ratio("top_bids_sniped", "top_bids"),
        "reused": ratio("reused", "later_person_days"),
        "later_balance": pair(lambda team: Mean(c[team]["later_balance"], c[team]["later_person_days"])),
        "last_day_balance": pair(lambda team: Mean(c[team]["last_day_balance"], c[team]["last_day_person_days"])),
        "end_coins": pair(lambda team: sum(coins for payload in matches
                                           for name, coins in payload["match"]["remaining_coins"].items()
                                           if team_of[name] == team)),
        # 队内撞车
        "team_collisions": pair(lambda team: collisions[team]["team_collisions"]),
        "intra_burn": pair(lambda team: c[team]["intra_burn"]),
        "all_burn": pair(lambda team: c[team]["all_burn"]),
        "keep_one_days": pair(lambda team: keep_one_days[team]),
        # 价表
        "first_day_tables": first_day_tables,
        "void_tables": void_tables,
    }
