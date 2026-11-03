"""AI 比赛各轮的编排：开局发言、规划、每天出价、局末发言、整理记忆和终局；私信在收件人的下一轮送达。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from auction_game.agent import prompts
from auction_game.agent.player import AIPlayer
from auction_game.flow.controller import Controller, RoundTask
from auction_game.game.settings import GameSettings
from auction_game.game.models import MatchLog, Observation, StandingsEntry
from auction_game.reporting.run_paths import RunPaths

# 节标题是写信的环节，说明送达时机，免得读成收信的时候。
LETTERS_HEADER = "# 私信\n\n每节是这一环节里写的信，收件人在下一次行动时收到。\n"


def _append(path: Path, text: str) -> None:
    with path.open("a", encoding="utf-8") as file:
        file.write(text)


class AIDirector:
    """每个环节由全员并发完成一轮任务，经过 controller 的单步与暂停控制。"""

    def __init__(self, players: list[AIPlayer], controller: Controller, log_dir: Path, *, game: GameSettings):
        self.players = players
        self.controller = controller
        self.paths = RunPaths(log_dir)
        self.game = game
        # 待送达的信：收件人到 {写信人: 正文}，写信人按配置顺序。
        self.mailbox: dict[str, dict[str, str]] = {}
        # 本局规划交出的第 1 天出价：玩家到 (正常出价, 超级出价)。
        self.day1_bids: dict[str, tuple[int, int | None]] = {}

    def _record(self, text: str) -> None:
        _append(self.paths.speeches, text + "\n")
        print(text)

    def _record_letters(self, label: str, written: dict[str, list[dict]]) -> None:
        """有人写信时追加一节，写信人按配置顺序；第一次有信时创建文件。终端不打印。"""
        letters = [f"【{sender} → {letter['to']}】{letter['text']}"
                   for sender, sent in written.items() for letter in sent]
        if not letters:
            return
        text = f"\n## {label}\n\n" + "\n\n".join(letters) + "\n"
        _append(self.paths.letters, text if self.paths.letters.exists() else LETTERS_HEADER + text)

    def _run_round(self, label: str, run: Callable[[AIPlayer, dict[str, str]], tuple[Any, list[dict]]]
                   ) -> dict[str, Any]:
        """全员执行 run(玩家, 收到的信)，run 返回 (结果, 写的信)；按配置中的玩家顺序返回结果。

        label 也是失败日志和 letters.md 的节标题。收到的信在轮开始前取出，补跑时不变。controller.run_round
        只在整轮成功并确认后返回，这时才记下这一轮写的信、再用它们替换邮箱，所以退出时邮箱仍是轮开始时的状态。
        """
        inbox = self.mailbox
        tasks = [RoundTask(key=player.name, participant=player,
                           fn=lambda player=player: run(player, inbox.get(player.name, {})))
                 for player in self.players]
        results = self.controller.run_round(tasks, label)
        written = {player.name: results[player.name][1] for player in self.players}
        self._record_letters(label, written)
        mailbox: dict[str, dict[str, str]] = {}
        for sender, sent in written.items():
            for letter in sent:
                mailbox.setdefault(letter["to"], {})[sender] = letter["text"]
        self.mailbox = mailbox
        return {player.name: results[player.name][0] for player in self.players}

    @staticmethod
    def _others(speeches: dict[str, str], exclude: str) -> dict[str, str]:
        return {name: text for name, text in speeches.items() if name != exclude}

    def _publish_speeches(self, title: str, speeches: dict[str, str]) -> None:
        self._record("\n".join([f"\n===== {title} =====", *(f"【{name}】{text}" for name, text in speeches.items())]))

    def collect_bids(self, observations: list[tuple[AIPlayer, Observation]]) -> list[tuple[int, int | None]]:
        """引擎的 bid_collector：按 observations 的顺序返回出价。

        第 1 天用本局规划交出的出价，不开轮次，规划时写的信留到下一次请求；第 2 天起全员出价一轮。
        """
        sample = observations[0][1]  # 同一轮内所有玩家的局/天相同
        if sample.day == 1:
            return [self.day1_bids[player.name] for player, _ in observations]
        by_name = {player.name: obs for player, obs in observations}
        bids = self._run_round(f"第 {sample.match_index} 局第 {sample.day} 天出价",
                               lambda p, received: p.bid(by_name[p.name], received))
        return [bids[player.name] for player, _ in observations]

    def on_opening(self) -> None:
        label = "开局发言"
        speeches = self._run_round(label, lambda p, _: p.give_speech(
            prompts.opening_speech_request(), "opening_speech", label))
        self._publish_speeches(label, speeches)
        plan_label = "第 1 局策略规划（含首日出价）"
        self.day1_bids = self._run_round(plan_label, lambda p, received: p.plan_strategy(
            prompts.opening_plan_request(self._others(speeches, p.name), self.game, received=received),
            "opening_plan", plan_label))

    def on_match_end(self, logs: list[MatchLog], standings: list[StandingsEntry]) -> None:
        log = logs[-1]
        index = log.match_index
        label = f"第 {index} 局局末发言"
        speeches = self._run_round(label, lambda p, received: p.give_speech(
            prompts.match_end_speech_request(log, standings, received=received), f"match_{index}_speech", label))
        self._publish_speeches(label, speeches)
        memory_label = f"第 {index} 局累计私有记忆"
        self._run_round(memory_label, lambda p, received: (p.save_memory(
            prompts.memory_request(self._others(speeches, p.name), index, received=received),
            f"match_{index}_memory", memory_label), []))
        plan_label = f"第 {index + 1} 局策略规划（含首日出价）"
        # 整理记忆不写信，这一轮收不到信。
        self.day1_bids = self._run_round(plan_label, lambda p, _: p.plan_strategy(
            prompts.next_match_plan_request(logs, standings, self.game), f"match_{index}_plan", plan_label))

    def on_final(self, standings: list[StandingsEntry], log: MatchLog) -> None:
        label = "最终感言"
        speeches = self._run_round(label, lambda p, received: p.give_speech(
            prompts.final_speech_request(standings, log, received=received), "final_speech", label,
            letters=False))
        self._publish_speeches(label, speeches)
        summary_label = "观众总结（仅观众可见）"
        summaries = self._run_round(summary_label, lambda p, _: (p.final_summary(
            prompts.final_summary_request(self._others(speeches, p.name)), summary_label), []))
        self._publish_speeches(summary_label, summaries)
