"""开赛前按以往场次的实际用量预估一场的花费；只接收阵容、比赛参数和历史场次目录。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from statistics import median

from auction_game.agent.log import read_records
from auction_game.game.settings import DISCUSSION_ROUNDS, GameSettings
from auction_game.reporting.analyze import TASK_LABELS
from auction_game.reporting.cost import dollars, response_cost
from auction_game.reporting.run_paths import RunPaths

USAGE_FIELDS = ("input_tokens", "cache_read_tokens", "cache_write_tokens", "cache_write_1h_tokens", "output_tokens")
NOTE = "含平均纠错，不含加赛、请求失败重发和补跑，只看量级"


def task_counts(game: GameSettings, *, team: bool) -> dict[str, int]:
    """正赛打满时每个席位各类任务的次数，不含加赛。

    M 局正赛、每局 D 天：开局发言、局末发言（M−1 次）和最终感言都是发言；每局一次规划交第 1 天出价，
    其余 D−1 天各一次出价；除最后一局外每局整理一次记忆；最后一次观众总结；团队赛每局开局讨论若干轮。
    """
    matches, days = game.regular_matches, game.max_days
    counts = {"speech": matches + 1, "plan": matches, "bid": matches * (days - 1),
              "memory": matches - 1, "summary": 1}
    if team:
        counts["discussion"] = DISCUSSION_ROUNDS * matches
    return {task: counts[task] for task in TASK_LABELS if task in counts}


@dataclass(frozen=True)
class SeatModel:
    """阵容里的一个执行模型：key 和 effort 是目录键和推理档位，cost 是官方单价（没有时为 None）。"""

    key: str
    effort: str
    label: str
    seats: int
    cost: dict | None
    multiplier: int | float


@dataclass
class History:
    """一种赛制里一个模型的历史：runs 是场次目录名，tasks 是任务类型到每次成功任务的用量。

    一次任务的用量是它各条回复（含纠错往返）之和，replies 是回复条数。
    """

    runs: set[str] = field(default_factory=set)
    tasks: dict[str, list[dict[str, int]]] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelEstimate:
    """low、high 是这些席位合计的美元花费，全部任务都有历史时相等；无单价或无从估算时为 None。

    runs 是所用历史的场次数，borrowed 表示用了另一种赛制的历史，guessed 是没有历史、按全部模型估区间的任务，
    skipped 是所有模型都没有历史、没计入的任务。
    """

    model: SeatModel
    low: float | None
    high: float | None
    runs: int
    borrowed: bool
    guessed: tuple[str, ...]
    skipped: tuple[str, ...]


def collect_history(run_dirs: list[Path]) -> dict[tuple[str, str, bool], History]:
    """按（目录键、推理档位、是否团队赛）汇总历史场次里每次成功任务的用量；强关留下的半行不读。"""
    history: dict[tuple[str, str, bool], History] = {}
    for run_dir in run_dirs:
        paths = RunPaths(run_dir)
        snapshot = paths.read_config()
        team = any(pc["team"] for pc in snapshot["players"])
        for pc in snapshot["players"]:
            execution = pc["execution_model"]
            item = history.setdefault((execution["key"], execution["capabilities"]["reasoning_status"], team),
                                      History())
            item.runs.add(run_dir.name)
            for entry in read_records(paths.player(pc["number"]))[0]:
                if entry["task"] == "system" or entry["status"] != "success":
                    continue
                usage = dict.fromkeys((*USAGE_FIELDS, "replies"), 0)
                for message in entry["messages"]:
                    if message["kind"] == "response":
                        usage["replies"] += 1
                        for name in USAGE_FIELDS:
                            usage[name] += message["usage"].get(name, 0)
                item.tasks.setdefault(entry["task"], []).append(usage)
    return history


def _average(samples: list[dict[str, int]]) -> tuple[dict[str, float], float]:
    """平均每条回复的用量和平均每次任务的回复条数。分档按单条回复的输入判断，所以按条平均。"""
    replies = sum(sample["replies"] for sample in samples)
    return ({name: sum(sample[name] for sample in samples) / replies for name in USAGE_FIELDS},
            replies / len(samples))


def _model_averages(history: dict[tuple[str, str, bool], History], task: str,
                    team: bool) -> list[tuple[dict[str, float], float]]:
    """某种赛制里各模型这类任务的 _average；这种赛制里没有时用另一种。"""
    for mode in (team, not team):
        averages = [_average(item.tasks[task]) for (_, _, item_team), item in history.items()
                    if item_team == mode and item.tasks.get(task)]
        if averages:
            return averages
    return []


def estimate(models: list[SeatModel], game: GameSettings, *, team: bool,
             history: dict[tuple[str, str, bool], History]) -> list[ModelEstimate]:
    """每个模型的预估花费：每类任务按平均每条回复的用量计价，乘平均每次任务的回复条数和任务次数。

    用量优先取这个模型同赛制的历史平均，没有时借用另一种赛制；都没有时输入、缓存命中率和回复条数取全部模型
    同任务的中位数，输出取全部模型同任务平均输出的最小到最大，给出区间。所有模型都没有的任务不计入。
    """
    counts = task_counts(game, team=team)
    results = []
    for model in models:
        low = high = 0.0
        runs: set[str] = set()
        borrowed = False
        guessed, skipped = [], []
        for task, count in counts.items():
            own = next(((mode, item) for mode in (team, not team)
                        if (item := history.get((model.key, model.effort, mode))) and item.tasks.get(task)), None)
            if own is not None:
                mode, item = own
                runs |= item.runs
                borrowed |= mode != team
                usage, replies = _average(item.tasks[task])
                usages = [usage]
            else:
                averages = _model_averages(history, task, team)
                if not averages:
                    skipped.append(task)
                    continue
                guessed.append(task)
                input_tokens = median(usage["input_tokens"] for usage, _ in averages)
                hit_rate = median(usage["cache_read_tokens"] / usage["input_tokens"] for usage, _ in averages
                                  if usage["input_tokens"])
                outputs = [usage["output_tokens"] for usage, _ in averages]
                replies = median(replies for _, replies in averages)
                usages = [{"input_tokens": input_tokens, "cache_read_tokens": input_tokens * hit_rate,
                           "cache_write_tokens": 0, "output_tokens": output} for output in (min(outputs), max(outputs))]
            if model.cost is None:
                continue
            costs = [response_cost(usage, model.cost, model.multiplier) * replies * count * model.seats
                     for usage in usages]
            low += min(costs)
            high += max(costs)
        if model.cost is None or len(skipped) == len(counts):
            low = high = None
        results.append(ModelEstimate(model, low, high, len(runs), borrowed, tuple(guessed), tuple(skipped)))
    return results


def _span(low: float, high: float) -> str:
    return dollars(low) if dollars(low) == dollars(high) else f"{dollars(low)}–{dollars(high)}"


def format_estimate(estimates: list[ModelEstimate], game: GameSettings, *, team: bool) -> list[str]:
    """开赛时和 estimate 命令打印的预估。"""
    mode, other = ("团队赛", "个人赛") if team else ("个人赛", "团队赛")
    lines = [f"预估花费（{mode}正赛 {game.regular_matches} 局、每局 {game.max_days} 天，按当前单价和价格倍率）："]
    for item in estimates:
        model = item.model
        head = f"  {model.label}（{model.seats} 席）："
        if model.cost is None:
            lines.append(head + "无单价")
            continue
        if item.low is None:
            lines.append(head + "无法预估：logs/ 下没有可借用的历史场次")
            continue
        notes = []
        if not item.runs:
            notes.append("无历史，区间")
        else:
            notes.append(f"依据 {item.runs} 场" + (f"，借用{other}数据" if item.borrowed else ""))
            if item.guessed:
                notes.append(f"{'、'.join(TASK_LABELS[task] for task in item.guessed)}无历史，区间")
        if item.skipped:
            notes.append(f"{'、'.join(TASK_LABELS[task] for task in item.skipped)}无法预估，不计入")
        lines.append(head + f"{_span(item.low, item.high)}（{'；'.join(notes)}）")
    known = [item for item in estimates if item.low is not None]
    missing = [item.model.label for item in estimates if item.low is None]
    total = _span(sum(item.low for item in known), sum(item.high for item in known)) if known else "—"
    lines.append(f"  全场合计：{total}" + (f"（不含 {'、'.join(missing)}）" if missing else ""))
    lines.append(f"  {NOTE}")
    return lines
