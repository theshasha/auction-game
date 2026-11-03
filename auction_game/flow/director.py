"""整场 AI 比赛的流程：开局发言、规划、每天出价、局末发言、整理记忆和终局；私信在收件人的下一轮送达。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from auction_game.agent import prompts
from auction_game.agent.player import AIPlayer
from auction_game.flow.controller import Controller, RoundTask
from auction_game.game.engine import MatchState, compute_standings
from auction_game.game.settings import GameSettings
from auction_game.game.models import MatchLog, Observation, StandingsEntry
from auction_game.game.tournament import TournamentResult, has_next_match
from auction_game.reporting.run_output import print_day_result, publish_match, publish_speeches, record_letters
from auction_game.reporting.run_paths import RunPaths

Bid = tuple[int, int | None]  # (正常出价, 超级出价或 None)


class AIDirector:
    """每个环节由全员并发完成一轮任务，经过 controller 的单步与暂停控制。"""

    def __init__(self, players: list[AIPlayer], controller: Controller, log_dir: Path, *, game: GameSettings):
        self.players = players
        self.controller = controller
        self.paths = RunPaths(log_dir)
        self.game = game
        # 待送达的信：收件人到 {写信人: 正文}，写信人按配置顺序。
        self.mailbox: dict[str, dict[str, str]] = {}

    def run(self) -> TournamentResult:
        """按顺序跑完整场比赛，返回最终结果。

        每局第 1 天用规划交出的出价结算，第 2 天起每天一轮出价；每局结束先公布结果，还有下一局时
        再做局末发言、整理记忆和下一局规划，最后一局之后是最终感言和观众总结。
        """
        names = [player.name for player in self.players]
        logs: list[MatchLog] = []
        day1_bids = self.opening()
        while True:
            match = MatchState(names, len(logs) + 1, len(logs) >= self.game.regular_matches, logs, self.game)
            for day in range(1, self.game.max_days + 1):
                observations = match.start_day()
                match.settle(day1_bids if day == 1 else self.bid_round(observations))
                print_day_result(match.log, match.standings())
            logs.append(match.log)
            standings = compute_standings(names, logs)
            publish_match(self.paths, match.log, standings)
            if not has_next_match(logs, standings, self.game):
                break
            day1_bids = self.match_end(logs, standings)
        self.final(standings, logs[-1])
        return TournamentResult(logs, standings)

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
        record_letters(self.paths, label, [(sender, letter["to"], letter["text"])
                                           for sender, sent in written.items() for letter in sent])
        mailbox: dict[str, dict[str, str]] = {}
        for sender, sent in written.items():
            for letter in sent:
                mailbox.setdefault(letter["to"], {})[sender] = letter["text"]
        self.mailbox = mailbox
        return {player.name: results[player.name][0] for player in self.players}

    @staticmethod
    def _others(speeches: dict[str, str], exclude: str) -> dict[str, str]:
        return {name: text for name, text in speeches.items() if name != exclude}

    def bid_round(self, observations: dict[str, Observation]) -> dict[str, Bid]:
        """第 2 天起的出价轮；observations 是玩家到当天能看到的信息。"""
        sample = next(iter(observations.values()))  # 同一轮内所有玩家的局/天相同
        return self._run_round(f"第 {sample.match_index} 局第 {sample.day} 天出价",
                               lambda p, received: p.bid(observations[p.name], received))

    def opening(self) -> dict[str, Bid]:
        """开局发言和第 1 局规划，返回规划交出的第 1 天出价。"""
        label = "开局发言"
        speeches = self._run_round(label, lambda p, _: p.give_speech(
            prompts.opening_speech_request(), "opening_speech", label))
        publish_speeches(self.paths, label, speeches)
        plan_label = "第 1 局策略规划（含首日出价）"
        return self._run_round(plan_label, lambda p, received: p.plan_strategy(
            prompts.opening_plan_request(self._others(speeches, p.name), self.game, received=received),
            "opening_plan", plan_label))

    def match_end(self, logs: list[MatchLog], standings: list[StandingsEntry]) -> dict[str, Bid]:
        """还有下一局时的局末发言、整理记忆和下一局规划，返回规划交出的第 1 天出价；logs 是已结束的全部局。"""
        log = logs[-1]
        index = log.match_index
        label = f"第 {index} 局局末发言"
        speeches = self._run_round(label, lambda p, received: p.give_speech(
            prompts.match_end_speech_request(log, standings, received=received), f"match_{index}_speech", label))
        publish_speeches(self.paths, label, speeches)
        memory_label = f"第 {index} 局累计私有记忆"
        self._run_round(memory_label, lambda p, received: (p.save_memory(
            prompts.memory_request(self._others(speeches, p.name), index, received=received),
            f"match_{index}_memory", memory_label), []))
        plan_label = f"第 {index + 1} 局策略规划（含首日出价）"
        # 整理记忆不写信，这一轮收不到信。
        return self._run_round(plan_label, lambda p, _: p.plan_strategy(
            prompts.next_match_plan_request(logs, standings, self.game), f"match_{index}_plan", plan_label))

    def final(self, standings: list[StandingsEntry], log: MatchLog) -> None:
        """最后一局之后的最终感言和观众总结；最后一局的出价日志随感言请求发出。"""
        label = "最终感言"
        speeches = self._run_round(label, lambda p, received: p.give_speech(
            prompts.final_speech_request(standings, log, received=received), "final_speech", label,
            letters=False))
        publish_speeches(self.paths, label, speeches)
        summary_label = "观众总结（仅观众可见）"
        summaries = self._run_round(summary_label, lambda p, _: (p.final_summary(
            prompts.final_summary_request(self._others(speeches, p.name)), summary_label), []))
        publish_speeches(self.paths, summary_label, summaries)
