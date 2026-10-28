"""AI 开局、局末、下局规划和终局发言编排。"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from auction_game.ai import prompts
from auction_game.ai.ai_player import AIPlayer
from auction_game.controller import Controller, RoundTask, run_parallel_round
from auction_game.game.game_rules import GameSettings
from auction_game.game.models import MatchLog, StandingsEntry


class AISpeechDirector:
    """每个环节由全员并发完成一轮任务，经过 controller 的单步与暂停控制。"""

    def __init__(self, players: list[AIPlayer], controller: Controller, log_dir: Path, *, game: GameSettings):
        self.players = players
        self.controller = controller
        self.speeches_log = log_dir / "speeches.md"
        self.game = game

    def _record(self, text: str) -> None:
        with self.speeches_log.open("a", encoding="utf-8") as file:
            file.write(text + "\n")
        print(text)

    def _run_round(self, label: str, run: Callable[[AIPlayer], str | None]) -> dict[str, str | None]:
        """全员执行 run，按配置中的玩家顺序返回结果；label 也是失败日志的轮次说明。"""
        tasks = [RoundTask(key=player.name, participant=player, fn=lambda player=player: run(player))
                 for player in self.players]
        results = run_parallel_round(tasks, self.controller, label=label)
        return {player.name: results[player.name] for player in self.players}

    @staticmethod
    def _others(speeches: dict[str, str], exclude: str) -> dict[str, str]:
        return {name: text for name, text in speeches.items() if name != exclude}

    def _publish_speeches(self, title: str, speeches: dict[str, str]) -> None:
        self._record("\n".join([f"\n===== {title} =====", *(f"【{name}】{text}" for name, text in speeches.items())]))

    def on_opening(self) -> None:
        label = "开局发言"
        speeches = self._run_round(label, lambda p: p.give_speech(
            prompts.opening_speech_request(), "opening_speech", label))
        self._publish_speeches(label, speeches)
        plan_label = "第 1 局策略规划（含首日出价）"
        self._run_round(plan_label, lambda p: p.plan_strategy(
            prompts.opening_plan_request(self._others(speeches, p.name), self.game), "opening_plan", plan_label))

    def on_match_end(self, logs: list[MatchLog], standings: list[StandingsEntry]) -> None:
        log = logs[-1]
        index = log.match_index
        label = f"第 {index} 局局末发言"
        speeches = self._run_round(label, lambda p: p.give_speech(
            prompts.match_end_speech_request(log, standings), f"match_{index}_speech", label))
        self._publish_speeches(label, speeches)
        memory_label = f"第 {index} 局累计私有记忆"
        self._run_round(memory_label, lambda p: p.save_memory(
            prompts.memory_request(self._others(speeches, p.name), index), f"match_{index}_memory", memory_label))
        plan_label = f"第 {index + 1} 局策略规划（含首日出价）"
        self._run_round(plan_label, lambda p: p.plan_strategy(
            prompts.next_match_plan_request(logs, standings, self.game), f"match_{index}_plan", plan_label))

    def on_final(self, standings: list[StandingsEntry], log: MatchLog) -> None:
        label = "最终感言"
        speeches = self._run_round(label, lambda p: p.give_speech(
            prompts.final_speech_request(standings, log), "final_speech", label))
        self._publish_speeches(label, speeches)
        summary_label = "观众总结（仅观众可见）"
        summaries = self._run_round(summary_label, lambda p: p.final_summary(
            prompts.final_summary_request(self._others(speeches, p.name)), summary_label))
        self._publish_speeches(summary_label, summaries)
