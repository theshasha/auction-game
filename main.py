"""入口：默认 RandomBot 整场；--ai 进入 Gemini 交互模式（单步/连续/暂停）。"""

from __future__ import annotations

import argparse
import json
import random
from dataclasses import asdict
from pathlib import Path

import yaml

import prompts
from ai_player import AIPlayer
from controller import (
    Controller,
    RoundAborted,
    RoundTask,
    TournamentAborted,
    run_parallel_round,
)
from llm_client import MissingApiKeyError, make_chat_fn
from models import MatchLog, StandingsEntry
from players import RandomBot
from tournament import run_tournament

BOT_NAMES = ["Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot"]
ROOT_DIR = Path(__file__).parent
LOG_DIR = ROOT_DIR / "logs"
CONFIG_PATH = ROOT_DIR / "config.yaml"

END_REASON_LABELS = {
    "wins_exhausted": "3 个胜场全部拍出",
    "max_days": "达到第 5 天上限",
    "all_coins_zero": "全员余额归零",
}

ABSENT_SPEECH = "（放弃发言）"


def make_publish(log_dir: Path):
    """局末公开：完整出价日志 + 截至当前的累计排名与胜场明细，写 json/txt 并打印。"""

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
            f"{prompts.render_match_log(log)}\n"
            f"  结束原因: {END_REASON_LABELS.get(log.end_reason, log.end_reason)}\n\n"
            f"截至本局的累计排名:\n{prompts.render_standings(standings)}\n"
        )
        (log_dir / f"match_{log.match_index}.txt").write_text(text, encoding="utf-8")
        print(f"第 {log.match_index} 局结束（{kind}），日志已写入 logs/match_{log.match_index}.json/.txt")

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

    def __init__(self, players: list[AIPlayer], controller: Controller):
        self.players = players
        self.controller = controller
        self.speeches_log = LOG_DIR / "speeches.txt"

    def _record(self, text: str) -> None:
        with self.speeches_log.open("a", encoding="utf-8") as file:
            file.write(text + "\n")
        print(text)

    @staticmethod
    def _dispatch(player: AIPlayer, request: tuple[str, str]):
        user_text, phase = request
        if phase == "final_summary":
            return player.final_summary(user_text)
        if "plan" in phase:
            return player.plan_strategy(user_text, phase)
        return player.give_speech(user_text, phase)

    def _run_round(self, requests: dict[AIPlayer, tuple[str, str]], phase_label: str) -> dict[str, str | None]:
        """requests: {player: (user_text, phase)}。返回 {名字: 发言或 None}。
        暂停回滚后清标志并原地重跑本轮；中止比赛则向上抛。"""
        tasks = [
            RoundTask(
                key=player.name,
                description=f"{player.name} {phase_label}",
                player=player,
                fn=lambda player=player, req=req: self._dispatch(player, req),
            )
            for player, req in requests.items()
        ]
        while True:
            try:
                return run_parallel_round(tasks, self.controller)
            except RoundAborted:
                self.controller.clear_pause()
                continue

    @staticmethod
    def _others(speeches: dict[str, str | None], exclude: str) -> dict[str, str]:
        return {
            name: (text if text is not None else ABSENT_SPEECH)
            for name, text in speeches.items()
            if name != exclude
        }

    def _publish_speeches(self, title: str, speeches: dict[str, str | None]) -> None:
        lines = [f"\n===== {title} ====="]
        for name, text in speeches.items():
            lines.append(f"【{name}】{text if text is not None else ABSENT_SPEECH}")
        self._record("\n".join(lines))

    def on_opening(self) -> None:
        speeches = self._run_round(
            {p: (prompts.opening_speech_request(), "opening_speech") for p in self.players},
            "开局发言",
        )
        self._publish_speeches("开局发言", speeches)
        self._run_round(
            {p: (prompts.opening_plan_request(self._others(speeches, p.name)), "opening_plan") for p in self.players},
            "第一局策略规划",
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
                "下一局策略规划",
            )

    def on_final(self, standings: list[StandingsEntry]) -> None:
        speeches = self._run_round(
            {p: (prompts.final_speech_request(standings), "final_speech") for p in self.players},
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
    LOG_DIR.mkdir(exist_ok=True)
    result = run_tournament(players, on_match_end=make_publish(LOG_DIR))
    print_final_result(result)


def run_ai() -> None:
    config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    llm_config = config.get("llm", {})
    try:
        chat_fn = make_chat_fn(
            base_url=llm_config.get("base_url", "https://generativelanguage.googleapis.com/v1beta/openai/"),
            model=llm_config.get("model", "gemini-flash-lite-latest"),
            timeout=llm_config.get("timeout_seconds", 60),
        )
    except MissingApiKeyError as exc:
        print(f"错误：{exc}")
        return

    LOG_DIR.mkdir(exist_ok=True)
    controller = Controller(mode="step")

    def pause_on_double_error(detail: str) -> None:
        """连续两次调用失败：报错并暂停，本轮回滚后等用户确认重跑。"""
        controller.request_pause(detail)
        raise RoundAborted(detail)

    player_configs = config["players"]
    all_names = [pc["name"] for pc in player_configs]
    players = [
        AIPlayer(
            name=pc["name"],
            persona=pc["persona"],
            all_names=all_names,
            chat_fn=chat_fn,
            log_path=LOG_DIR / f"ai_{pc['name']}.jsonl",
            on_double_error=pause_on_double_error,
        )
        for pc in player_configs
    ]

    controller.start_stdin_reader()
    print("命令：回车=单步推进一次对话 / c=连续模式 / s=单步模式 / p=暂停并回滚本轮 / q=中止比赛")

    def bid_collector(observations):
        tasks = [
            RoundTask(
                key=player.name,
                description=f"{player.name} 第{len(obs.past_match_logs) + 1}局第{obs.day}天出价",
                player=player,
                fn=lambda player=player, obs=obs: player.decide_bid(obs),
            )
            for player, obs in observations
        ]
        while True:
            try:
                results = run_parallel_round(tasks, controller)
                return [results[player.name] for player, _ in observations]
            except RoundAborted:
                controller.clear_pause()
                continue

    director = AISpeechDirector(players, controller)
    try:
        result = run_tournament(
            players,
            on_match_end=make_publish(LOG_DIR),
            director=director,
            bid_collector=bid_collector,
        )
    except TournamentAborted:
        print("\n比赛已被用户中止。")
        return
    print_final_result(result)


def main() -> None:
    parser = argparse.ArgumentParser(description="唯一高价拍卖游戏")
    parser.add_argument("--seed", type=int, default=None, help="随机种子（仅 Bot 模式），用于复现对局")
    parser.add_argument("--ai", action="store_true", help="AI 模式：读取 config.yaml 人设，走 Gemini")
    args = parser.parse_args()

    if args.ai:
        run_ai()
    else:
        run_bots(args.seed)


if __name__ == "__main__":
    main()
