"""入口：默认 RandomBot 整场；--ai 进入 AI 交互模式（单步/连续/暂停），每玩家独立模型。"""

from __future__ import annotations

import argparse
import json
import random
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import yaml

import prompts
from ai_player import AIPlayer
from controller import (
    Controller,
    RoundTask,
    TournamentAborted,
    run_parallel_round,
)
from llm_client import MissingApiKeyError, make_chat_fn
from models import MatchLog, StandingsEntry
from players import RandomBot, ScriptedPlayer
from tournament import run_tournament

BOT_NAMES = ["Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot"]
ROOT_DIR = Path(__file__).parent
LOG_DIR = ROOT_DIR / "logs"
CONFIG_PATH = ROOT_DIR / "config.yaml"

ABSENT_SPEECH = "（放弃发言）"


def make_run_dir(mode: str) -> Path:
    """为本场运行创建独立日志目录 logs/<时间戳>_<模式>/，并更新 logs/latest 软链接。"""
    base_name = f"{datetime.now():%Y%m%d_%H%M%S}_{mode}"
    run_dir = LOG_DIR / base_name
    suffix = 2
    while run_dir.exists():
        run_dir = LOG_DIR / f"{base_name}_{suffix}"
        suffix += 1
    run_dir.mkdir(parents=True)

    latest_link = LOG_DIR / "latest"
    try:
        if latest_link.is_symlink() or latest_link.exists():
            latest_link.unlink()
        latest_link.symlink_to(run_dir.name)
    except OSError as exc:
        print(f"警告：更新 logs/latest 软链接失败（{exc}），不影响日志写入")

    print(f"本场日志目录：{run_dir.relative_to(ROOT_DIR)}")
    return run_dir


def make_publish(log_dir: Path):
    """局末公开：完整出价日志 + 截至当前的累计排名与胜场明细，写 json/md 并打印。"""

    def publish(log: MatchLog, standings: list[StandingsEntry]) -> None:
        payload = {
            "match": log.to_dict(),
            "standings_after_match": [asdict(entry) for entry in standings],
        }
        (log_dir / f"match_{log.match_index}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        kind = "加赛" if log.is_playoff else "正赛"
        text = (
            f"===== 第 {log.match_index} 局（{kind}）=====\n"
            f"{prompts.render_match_log(log)}\n\n"
            f"截至本局的累计排名:\n{prompts.render_standings(standings)}\n"
        )
        (log_dir / f"match_{log.match_index}.md").write_text(text, encoding="utf-8")
        match_log_path = (log_dir / f"match_{log.match_index}").relative_to(ROOT_DIR)
        print(f"第 {log.match_index} 局结束（{kind}），日志已写入 {match_log_path}.json/.md")

    return publish


def print_final_result(result) -> None:
    print(f"\n共进行 {len(result.match_logs)} 局（正赛 5 局 + 加赛 {result.playoff_count} 局）")
    if result.playoff_cap_hit:
        print("警告：加赛达到安全上限，第一名仍为并列，按当前总胜场结算")
    print("\n===== 最终排名 =====")
    print(prompts.render_standings(result.final_standings))
    champions = [e for e in result.final_standings if e.rank == 1]
    if len(champions) > 1:
        names = "、".join(e.player for e in champions)
        print(f"\n并列冠军: {names}（{champions[0].total_wins} 胜）")
    else:
        print(f"\n冠军: {champions[0].player}（{champions[0].total_wins} 胜）")


class AISpeechDirector:
    """发言编排：开局/局末/终局，所有环节都经过 controller 的单步与暂停控制。"""

    def __init__(self, players: list[AIPlayer], controller: Controller, log_dir: Path):
        self.players = players
        self.controller = controller
        self.speeches_log = log_dir / "speeches.md"

    def _record(self, text: str) -> None:
        with self.speeches_log.open("a", encoding="utf-8") as file:
            file.write(text + "\n")
        print(text)

    @staticmethod
    def _dispatch(player: AIPlayer, request: tuple[str, str], context: str) -> str:
        user_text, phase = request
        if phase == "final_summary":
            return player.final_summary(user_text, context)
        if "plan" in phase:
            return player.plan_strategy(user_text, phase, context)
        return player.give_speech(user_text, phase, context)

    def _run_round(self, requests: dict[AIPlayer, tuple[str, str]], phase_label: str) -> dict[str, str]:
        """requests: {player: (user_text, phase)}。返回 {名字: 发言}。
        phase_label 同时作为失败日志的轮次上下文（context）传给玩家。
        失败重跑与暂停补跑由 run_parallel_round 内部处理；中止比赛向上抛。"""
        tasks = [
            RoundTask(
                key=player.name,
                player=player,
                fn=lambda player=player, req=req: self._dispatch(player, req, phase_label),
            )
            for player, req in requests.items()
        ]
        return run_parallel_round(tasks, self.controller, label=phase_label)

    @staticmethod
    def _others(speeches: dict[str, str], exclude: str) -> dict[str, str]:
        return {name: text for name, text in speeches.items() if name != exclude}

    def _publish_speeches(self, title: str, speeches: dict[str, str]) -> None:
        lines = [f"\n===== {title} ====="]
        for name, text in speeches.items():
            lines.append(f"【{name}】{text if text else ABSENT_SPEECH}")
        self._record("\n".join(lines))

    def on_opening(self) -> None:
        speeches = self._run_round(
            {p: (prompts.opening_speech_request(), "opening_speech") for p in self.players},
            "开局发言",
        )
        self._publish_speeches("开局发言", speeches)
        self._run_round(
            {p: (prompts.opening_plan_request(self._others(speeches, p.name)), "opening_plan") for p in self.players},
            "第一局策略规划（含首日出价）",
        )

    def on_match_end(self, log: MatchLog, standings: list[StandingsEntry], will_continue: bool) -> None:
        speeches = self._run_round(
            {
                p: (prompts.match_end_speech_request(log, standings, will_continue), f"match_{log.match_index}_speech")
                for p in self.players
            },
            f"第 {log.match_index} 局局末发言",
        )
        self._publish_speeches(f"第 {log.match_index} 局局末发言", speeches)
        if will_continue:
            self._run_round(
                {p: (prompts.next_match_plan_request(self._others(speeches, p.name)), f"match_{log.match_index}_plan") for p in self.players},
                "下一局策略规划（含首日出价）",
            )

    def on_final(self, standings: list[StandingsEntry], log: MatchLog) -> None:
        speeches = self._run_round(
            {p: (prompts.final_speech_request(standings, log), "final_speech") for p in self.players},
            "最终感言",
        )
        self._publish_speeches("最终感言", speeches)
        summaries = self._run_round(
            {p: (prompts.final_summary_request(self._others(speeches, p.name)), "final_summary") for p in self.players},
            "观众总结（不回喂）",
        )
        lines = ["\n===== 观众总结（仅观众可见）====="]
        for name, text in summaries.items():
            lines.append(f"【{name}】{text if text else ABSENT_SPEECH}")
        self._record("\n".join(lines))


def run_bots(seed) -> None:
    rng = random.Random(seed)
    players = [RandomBot(name, random.Random(rng.random())) for name in BOT_NAMES]
    run_dir = make_run_dir("bot")
    result = run_tournament(players, on_match_end=make_publish(run_dir))
    print_final_result(result)


def run_ai() -> None:
    config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    llm_config = config.get("llm", {})
    timeout = llm_config.get("timeout_seconds", 60)

    player_configs = config["players"]
    all_names = [pc["name"] for pc in player_configs]
    predictions = [(pc["name"], pc.get("predicted_win_rate", 0)) for pc in player_configs]

    chat_fns = {}
    for pc in player_configs:
        try:
            chat_fns[pc["name"]] = make_chat_fn(
                provider=pc["provider"],
                model=pc["model"],
                timeout=timeout,
                protocol=pc.get("protocol"),
            )
        except (MissingApiKeyError, ValueError) as exc:
            print(f"错误：玩家 {pc['name']} 的 LLM 配置不可用：{exc}")
            return

    run_dir = make_run_dir("ai")
    controller = Controller(mode="step")

    players = [
        AIPlayer(
            name=pc["name"],
            persona_title=pc["persona_title"],
            persona_text=pc["persona_text"],
            predictions=predictions,
            all_names=all_names,
            chat_fn=chat_fns[pc["name"]],
            log_path=run_dir / f"ai_{pc['name']}.jsonl",
        )
        for pc in player_configs
    ]
    for pc in player_configs:
        print(f"{pc['name']}: {pc['provider']} / {pc['model']}（人设：{pc['persona_title']}）")

    controller.start_stdin_reader()
    print("命令：回车=推进一轮 / c=连续模式 / s=单步模式 / p=暂停（保留已完成，恢复后仅补跑未完成）/ q=中止比赛")

    def bid_collector(observations):
        sample_obs = observations[0][1]  # 同一轮内所有玩家的局/天相同
        label = f"第{len(sample_obs.past_match_logs) + 1}局第{sample_obs.day}天出价"
        tasks = [
            RoundTask(
                key=player.name,
                player=player,
                fn=lambda player=player, obs=obs: player.decide_bid(obs),
            )
            for player, obs in observations
        ]
        results = run_parallel_round(tasks, controller, label=label)
        return [results[player.name] for player, _ in observations]

    director = AISpeechDirector(players, controller, run_dir)
    try:
        result = run_tournament(
            players,
            on_match_end=make_publish(run_dir),
            director=director,
            bid_collector=bid_collector,
        )
    except TournamentAborted:
        print("\n比赛已被用户中止。")
        return
    print_final_result(result)


def run_benchmark(count: int) -> None:
    """规则平衡性验证（不写日志）：count 场纯随机 Bot 对战 + count 场鲸鱼探针对照。

    鲸鱼探针：1 个恒出 6 的 ScriptedPlayer + 5 个 RandomBot。若鲸鱼平均总胜场
    显著高于随机对手，说明「出最大数字」仍是无脑占优策略，规则需要再调。
    """
    from collections import Counter

    def play_tournament(seed: int, with_whale: bool):
        rng = random.Random(seed)
        players: list = []
        if with_whale:
            players.append(ScriptedPlayer.constant("Whale", 6))
        for name in BOT_NAMES[: 5 if with_whale else 6]:
            players.append(RandomBot(name, random.Random(rng.random())))
        return run_tournament(players)

    def aggregate(results) -> dict:
        stats = {
            "days": 0,
            "void_days": 0,
            "winning_bids": Counter(),
            "bids_by_day": {},
            "pools_by_day": {},
            "playoffs": 0,
        }
        for result in results:
            stats["playoffs"] += result.playoff_count
            for log in result.match_logs:
                for day in log.days:
                    stats["days"] += 1
                    stats["pools_by_day"].setdefault(day.day, []).append(day.pool)
                    if day.is_void:
                        stats["void_days"] += 1
                        continue
                    stats["winning_bids"][day.winning_bid] += 1
                    stats["bids_by_day"].setdefault(day.day, []).extend(
                        record.bid for record in day.bids
                    )
        return stats

    def print_stats(label: str, stats: dict) -> None:
        days = stats["days"]
        decided = days - stats["void_days"]
        print(f"\n===== {label} =====")
        print(f"总拍卖天数：{days}，流局 {stats['void_days']} 天（{stats['void_days'] / days:.1%}），"
              f"加赛总局数 {stats['playoffs']}")
        print("每天平均出价 / 平均奖池：")
        for day_index in sorted(stats["pools_by_day"]):
            bids = stats["bids_by_day"].get(day_index, [])
            pools = stats["pools_by_day"][day_index]
            avg_bid = f"{sum(bids) / len(bids):.2f}" if bids else "—（全流局）"
            print(f"  第 {day_index} 天：出价 {avg_bid}，奖池 {sum(pools) / len(pools):.2f}")
        win_dist = dict(sorted(stats["winning_bids"].items()))
        cap_wins = stats["winning_bids"][6]
        print(f"获胜价分布：{win_dist}")
        print(f"获胜价=6（极点）占比：{cap_wins}/{decided}（{cap_wins / max(decided, 1):.1%}）")

    baseline = [play_tournament(seed=1000 + i, with_whale=False) for i in range(count)]
    print_stats(f"基准：{count} 场纯随机 Bot 对战", aggregate(baseline))

    probe = [play_tournament(seed=2000 + i, with_whale=True) for i in range(count)]
    print_stats(f"鲸鱼探针：{count} 场「Whale 恒出 6 + 5 随机」", aggregate(probe))
    whale_wins = []
    field_wins = []
    whale_ranks = []
    for result in probe:
        for entry in result.final_standings:
            if entry.player == "Whale":
                whale_wins.append(entry.total_wins)
                whale_ranks.append(entry.rank)
            else:
                field_wins.append(entry.total_wins)
    avg_whale = sum(whale_wins) / len(whale_wins)
    avg_field = sum(field_wins) / len(field_wins)
    avg_rank = sum(whale_ranks) / len(whale_ranks)
    print(f"Whale 平均总胜场 {avg_whale:.2f}（平均排名 {avg_rank:.2f}），"
          f"随机对手平均 {avg_field:.2f}")
    if avg_whale > avg_field * 1.2:
        print("判定：鲸鱼显著领先——「出最大数字」仍是占优策略，规则需要再调。")
    else:
        print("判定：鲸鱼未能拉开差距——价格约束生效，极点已拆除。")


def main() -> None:
    parser = argparse.ArgumentParser(description="唯一高价拍卖游戏")
    parser.add_argument("--seed", type=int, default=None, help="随机种子（仅 Bot 模式），用于复现对局")
    parser.add_argument("--ai", action="store_true", help="AI 模式：读取 config.yaml，每玩家走各自的 provider/model")
    parser.add_argument("--benchmark", type=int, metavar="N", default=None,
                        help="平衡性验证：跑 N 场纯随机 + N 场鲸鱼探针对照（不写日志）")
    args = parser.parse_args()

    if args.benchmark:
        run_benchmark(args.benchmark)
    elif args.ai:
        run_ai()
    else:
        run_bots(args.seed)


if __name__ == "__main__":
    main()
