"""打印团队赛两队对比的指标，一场或多场，口径见 docs/analyze.md 的"团队赛对比"。

用法：
    python -m auction_game.cli.team_report
    python -m auction_game.cli.team_report logs/<目录名> [logs/<目录名> ...]

不传目录时选最新的真实团队赛场次（team 或 team-test），离线赛要显式指定。
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
import sys

from auction_game.reporting.analyze import find_latest_ai_run
from auction_game.reporting.team_metrics import Mean, Ratio, collect_team_metrics, load_team_run


@dataclass(frozen=True)
class Row:
    label: str
    value: Callable[[dict], Any]
    percent: bool = False
    summable: bool = True   # 多场时能否把各场的值相加成合计列


def _metric(key):
    return lambda metrics: metrics[key]


def _format(value, percent=False) -> str:
    if value is None:
        return "—"
    if isinstance(value, tuple):
        return ":".join(_format(item, percent) for item in value)
    if isinstance(value, Ratio):
        text = f"{value.part}/{value.whole}"
        return f"{text}（{value.part / value.whole:.0%}）" if percent and value.whole else text
    if isinstance(value, Mean):
        return f"{value.value:.2f}" if value.value is not None else "—"
    return str(value)


def _sum(values):
    if any(value is None for value in values):
        return None
    if isinstance(values[0], tuple):
        return tuple(_sum([value[index] for value in values]) for index in range(len(values[0])))
    total = values[0]
    for value in values[1:]:
        total = total + value
    return total


def _best_fixed(metrics):
    return tuple(f"{'、'.join(map(str, prices))}（{days.part}/{days.whole}）"
                 for prices, days in metrics["best_fixed_price"])


def _keep_one_detail(metrics):
    return tuple("、".join(f"第 {match} 局第 {day} 天 {points} 分" for match, day, points in days) or "无"
                 for days in metrics["keep_one_days"])


def _score_diff(metrics):
    diff, low, high = metrics["score_diff"]
    return f"{diff}（{low} 到 {high}）"


def _bids(bids):
    return "、".join(map(str, bids))


def _by_day_rows(runs: list[dict]) -> list[Row]:
    numbers = sorted({number for metrics in runs for number in metrics["score_by_day"]})
    rows = []
    for number in numbers:
        rows.append(Row(f"第 {number} 天得分", lambda m, n=number: m["score_by_day"].get(n)))
        rows.append(Row(f"第 {number} 天赢下的天数", lambda m, n=number: m["days_won_by_day"].get(n)))
    return rows


def _first_day_table_rows(runs: list[dict]) -> list[Row]:
    def table(metrics, match):
        for index, first, second, overlap in metrics["first_day_tables"]:
            if index == match:
                return f"{_bids(first)}:{_bids(second)}（重合 {overlap}）"
        return None
    matches = sorted({entry[0] for metrics in runs for entry in metrics["first_day_tables"]})
    return [Row(f"第 {match} 局第 1 天", lambda m, match=match: table(m, match), summable=False)
            for match in matches]


def _void_rows(runs: list[dict]) -> list[Row]:
    def table(metrics, key):
        for match, day, first, second in metrics["void_tables"]:
            if (match, day) == key:
                return f"{_bids(first)}:{_bids(second)}"
        return None
    keys = sorted({(match, day) for metrics in runs for match, day, *_ in metrics["void_tables"]})
    rows = [Row(f"第 {match} 局第 {day} 天", lambda m, key=(match, day): table(m, key), summable=False)
            for match, day in keys]
    return rows or [Row("没有流局", lambda m: "—", summable=False)]


def _sections(runs: list[dict]) -> list[tuple[str, list[Row]]]:
    m = _metric
    return [
        ("得分", [
            Row("总分", m("total_score")),
            Row("拍卖分", m("auction_score")),
            Row("命中分", m("hit_score")),
            Row("赢下的天数", m("days_won")),
            *_by_day_rows(runs),
            Row("第 2 天起得分", m("later_score")),
            Row("第 1 天平均正常出价", m("first_day_bid")),
            Row("第 1 天获胜价均值（两队合并）", m("first_day_winning_bid")),
            Row("总分差（前队减后队）和按天重抽样的 95% 区间", _score_diff, summable=False),
        ]),
        ("超级出价", [
            Row("使用（人天）", m("super_uses"), percent=True),
            Row("使用率（两队合并）", m("super_uses_combined"), percent=True),
            Row("命中", m("super_hits"), percent=True),
            Row("第 1 天开枪", m("first_day_supers")),
            Row("净金币（悬赏减成本）", m("super_net_coins")),
            Row("拍卖分：去掉全部超级出价重算当天", m("auction_without_all")),
            Row("拍卖分：只去掉前队的超级出价", m("auction_without_first")),
            Row("拍卖分：只去掉后队的超级出价", m("auction_without_second")),
            Row("超级出价带来的拍卖分（实际减去全部去掉）", m("super_auction_gain")),
            Row("超级出价把当天从对方换成本队（天）", m("switched_to_own")),
            Row("  这些天的奖池", m("switched_to_own_points")),
            Row("超级出价让本队获胜的当天变成流局（天）", m("win_to_void")),
            Row("打余额价（占全部超级出价）", m("balance_shots")),
            Row("  其中命中", m("balance_hits"), percent=True),
            Row("  打出价上限的命中", m("balance_max")),
            Row("  第 1 天、不是出价上限的命中", m("balance_first_day")),
            Row("  第 2 天起、按余额读出的命中", m("balance_read")),
            Row("命中目标是余额价（占命中）", m("target_balance")),
            Row("  其中目标出价等于出价上限", m("target_max")),
            Row("  其中第 1 天、不是出价上限", m("target_first_day")),
            Row("  其中第 2 天起、按余额读出", m("target_read")),
            Row("和队友打同一价的命中", m("group_shots"), percent=True),
            Row("单独打一价的命中", m("single_shots"), percent=True),
            Row("三人同打一价的命中天数", m("full_team_days")),
            Row("第 2 天起打对方昨天的价位（占第 2 天起的超级出价）", m("yesterday_shots")),
            Row("  其中命中", m("yesterday_hits")),
            Row("事后最佳固定狙价（对方当天有人出的天数）", _best_fixed, summable=False),
        ]),
        ("正常出价和金币", [
            Row("贴上限（人天）", m("capped"), percent=True),
            Row("贴上限时被狙", m("capped_sniped"), percent=True),
            Row("没贴上限时被狙", m("uncapped_sniped"), percent=True),
            Row("出价至少 5、贴上限时被狙", m("capped_high_sniped"), percent=True),
            Row("出价至少 5、没贴上限时被狙", m("uncapped_high_sniped"), percent=True),
            Row("余额不超过 9 时贴上限", m("low_balance_capped"), percent=True),
            Row("最高三档的正常出价（笔）", m("top_bids")),
            Row("  其中获胜", m("top_bids_won"), percent=True),
            Row("  其中被狙", m("top_bids_sniped"), percent=True),
            Row("第 2 天起复用昨天本队价位", m("reused")),
            Row("第 2 天起的平均余额（出价前）", m("later_balance")),
            Row("最后一天的平均余额（出价前）", m("last_day_balance")),
            Row("局末剩余金币", m("end_coins")),
        ]),
        ("队内撞车", [
            Row("次数", m("team_collisions")),
            Row("队内撞车烧掉的金币", m("intra_burn")),
            Row("全队撞车烧掉的金币（含跨队撞和被狙）", m("all_burn")),
            Row("只留一人重算后能拿下的天数", lambda metrics: tuple(map(len, metrics["keep_one_days"]))),
            Row("  这些天的奖池", lambda metrics: tuple(sum(points for *_, points in days)
                                                     for days in metrics["keep_one_days"])),
            Row("  明细", _keep_one_detail, summable=False),
        ]),
        ("每局第 1 天的正常出价", _first_day_table_rows(runs)),
        ("流局日的正常出价", _void_rows(runs)),
    ]


def _print_table(title: str, header: list[str], lines: list[list[str]]) -> None:
    print(title)
    print("| " + " | ".join(header) + " |")
    print("|" + " --- |" * len(header))
    for line in lines:
        print("| " + " | ".join(line) + " |")
    print()


def report(run_dirs: list[Path]) -> None:
    runs = [load_team_run(run_dir) for run_dir in run_dirs]
    metrics = [collect_team_metrics(run.matches, run.team_of) for run in runs]
    with_total = len(runs) > 1 and len({run.teams for run in runs}) == 1
    header = ["指标", *(run.name for run in runs), *(["合计"] if with_total else [])]

    print("团队赛对比：格子是 前队:后队，阵营按场次快照里玩家的顺序；比率写成 分子/分母，合计列分子分母各自相加")
    if len(runs) > 1 and not with_total:
        print("各场阵营名不同，不给合计")
    overview = [
        ["阵营", *(":".join(run.teams) for run in runs), *([":".join(runs[0].teams)] if with_total else [])],
        ["已结束的局数", *(f"{item['match_count']}（加赛 {item['playoff_count']}）" for item in metrics),
         *([f"{sum(item['match_count'] for item in metrics)}（加赛 {sum(item['playoff_count'] for item in metrics)}）"]
           if with_total else [])],
        ["天数", *(str(item["total_days"]) for item in metrics),
         *([str(sum(item["total_days"] for item in metrics))] if with_total else [])],
    ]
    _print_table("概况", header, overview)

    for title, rows in _sections(metrics):
        lines = []
        for row in rows:
            values = [row.value(item) for item in metrics]
            cells = [_format(value, row.percent) for value in values]
            if with_total:
                cells.append(_format(_sum(values), row.percent) if row.summable else "—")
            lines.append([row.label, *cells])
        _print_table(title, header, lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="团队赛两队对比的指标")
    parser.add_argument("run_dirs", nargs="*", type=Path,
                        help="团队赛场次目录，可以传多个；不传时选最新的真实团队赛场次")
    args = parser.parse_args()
    run_dirs = args.run_dirs or [find_latest_ai_run(("team",))]
    try:
        report(run_dirs)
    except (OSError, ValueError) as exc:
        sys.exit(str(exc))


if __name__ == "__main__":
    main()
