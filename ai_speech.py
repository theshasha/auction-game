"""AI 开局、局末、下局规划和终局发言编排。"""

from __future__ import annotations

from pathlib import Path

import prompts
from ai_player import AIPlayer
from controller import Controller, RoundTask, run_parallel_round
from game_rules import GameSettings
from models import MatchLog, StandingsEntry

ABSENT_SPEECH = "（放弃发言）"


class AISpeechDirector:
    """发言编排：开局/局末/终局，所有环节都经过 controller 的单步与暂停控制。"""

    def __init__(self, players: list[AIPlayer], controller: Controller, log_dir: Path, *, game: GameSettings):
        self.players = players
        self.controller = controller
        self.speeches_log = log_dir / "speeches.md"
        self.game = game

    def _record(self, text: str) -> None:
        with self.speeches_log.open("a", encoding="utf-8") as file:
            file.write(text + "\n")
        print(text)

    @staticmethod
    def _dispatch(player: AIPlayer, request: tuple[str, str], context: str) -> str | None:
        user_text, phase = request
        if phase == "final_summary":
            return player.final_summary(user_text, context)
        if "plan" in phase:
            return player.plan_strategy(user_text, phase, context)
        return player.give_speech(user_text, phase, context)

    def _run_round(self, requests: dict[AIPlayer, tuple[str, str]], phase_label: str) -> dict[str, str | None]:
        """返回公开发言或总结；规划任务返回 None，调用方仅等待完成。
        phase_label 同时作为失败日志的轮次上下文（context）传给玩家。
        失败重跑与暂停补跑由 run_parallel_round 内部处理；中止比赛向上抛。"""
        tasks = [
            RoundTask(
                key=player.name,
                participant=player,
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
            {p: (prompts.opening_plan_request(self._others(speeches, p.name), self.game), "opening_plan") for p in self.players},
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
                {p: (prompts.next_match_plan_request(self._others(speeches, p.name), self.game), f"match_{log.match_index}_plan") for p in self.players},
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
