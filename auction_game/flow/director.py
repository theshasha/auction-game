"""整场 AI 比赛的流程：开局发言、团队赛的开局讨论、规划、每天出价、局末发言、整理记忆和终局；信在收件人的下一轮送达。"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from auction_game.agent import prompts
from auction_game.agent.player import AIPlayer
from auction_game.agent.tasks import TaskOutput
from auction_game.agent.text_output import BOARD
from auction_game.flow.controller import Controller, RoundTask
from auction_game.game.engine import MatchState, compute_standings
from auction_game.game.settings import DISCUSSION_ROUNDS, GameSettings
from auction_game.game.models import MatchLog, Observation, StandingsEntry
from auction_game.game.tournament import TournamentResult, has_next_match
from auction_game.reporting.run_output import print_day_result, publish_match, publish_speeches, record_letters
from auction_game.reporting.run_paths import RunPaths

Bid = tuple[int, int | None]  # (正常出价, 超级出价或 None)


class AIDirector:
    """每个环节由全员并发完成一轮任务，经过 controller 的单步与暂停控制。"""

    def __init__(self, players: list[AIPlayer], controller: Controller, log_dir: Path, *, game: GameSettings,
                 team_of: dict[str, str] | None = None):
        """team_of 是团队赛的玩家到阵营，交给结算和排名；个人赛为 None。"""
        self.players = players
        self.controller = controller
        self.paths = RunPaths(log_dir)
        self.game = game
        self.team_of = team_of
        self.team = team_of is not None
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
            match = MatchState(names, len(logs) + 1, len(logs) >= self.game.regular_matches, logs, self.game,
                               team_of=self.team_of)
            for day in range(1, self.game.max_days + 1):
                observations = match.start_day()
                match.settle(day1_bids if day == 1 else self.bid_round(observations))
                print_day_result(match.log, match.standings())
            logs.append(match.log)
            standings = compute_standings(names, logs, team_of=self.team_of)
            publish_match(self.paths, match.log, standings)
            if not has_next_match(logs, standings, self.game):
                break
            day1_bids = self.match_end(logs, standings)
        self.final(standings, logs[-1])
        return TournamentResult(logs, standings)

    def _run_round(self, label: str, run: Callable[[AIPlayer, dict[str, str]], TaskOutput]) -> dict[str, TaskOutput]:
        """全员执行 run(玩家, 收到的信)；按配置中的玩家顺序返回各人的输出。

        label 是这一轮的标题，也是私人日志、失败公告和 letters.md 的节标题。收到的信在轮开始前取出，补跑时不变。
        controller.run_round 只在整轮成功并确认后返回，这时才记下这一轮写的信、再用它们替换邮箱，
        所以退出时邮箱仍是轮开始时的状态。
        """
        inbox = self.mailbox
        tasks = [RoundTask(key=player.name, participant=player,
                           fn=lambda player=player: run(player, inbox.get(player.name, {})))
                 for player in self.players]
        results = self.controller.run_round(tasks, label)
        outputs = {player.name: results[player.name] for player in self.players}
        letters = [(sender, letter["to"], letter["text"]) for sender, output in outputs.items()
                   for letter in output.letters]
        record_letters(self.paths, label, [(sender, self.team_of[sender] if to == BOARD else to, text)
                                           for sender, to, text in letters], team=self.team)
        mailbox: dict[str, dict[str, str]] = {}
        for sender, to, text in letters:
            for recipient in self._teammates(sender) if to == BOARD else [to]:
                mailbox.setdefault(recipient, {})[sender] = text
        self.mailbox = mailbox
        return outputs

    def _teammates(self, name: str) -> list[str]:
        return [player for player, team in self.team_of.items() if team == self.team_of[name] and player != name]

    @staticmethod
    def _others(speeches: dict[str, str], exclude: str) -> dict[str, str]:
        return {name: text for name, text in speeches.items() if name != exclude}

    def _speech_round(self, label: str, run: Callable[[AIPlayer, dict[str, str]], TaskOutput]) -> dict[str, str]:
        """发言或感言的一轮：公布并返回各人的公开发言。"""
        speeches = {name: output.speech for name, output in self._run_round(label, run).items()}
        publish_speeches(self.paths, label, speeches)
        return speeches

    def _submission_round(self, label: str, run: Callable[[AIPlayer, dict[str, str]], TaskOutput]) -> dict[str, Bid]:
        """出价或规划的一轮：返回各人交的出价。"""
        return {name: output.submission for name, output in self._run_round(label, run).items()}

    def bid_round(self, observations: dict[str, Observation]) -> dict[str, Bid]:
        """第 2 天起的出价轮；observations 是玩家到当天能看到的信息。"""
        sample = next(iter(observations.values()))  # 同一轮内所有玩家的局/天相同
        label = f"第 {sample.match_index} 局第 {sample.day} 天出价"
        return self._submission_round(label, lambda p, received: p.bid(observations[p.name], received, label))

    def opening(self) -> dict[str, Bid]:
        """开局发言、团队赛的开局讨论和第 1 局规划，返回规划交出的第 1 天出价。"""
        label = "开局发言"
        speeches = self._speech_round(label, lambda p, _: p.give_speech(
            prompts.opening_speech_request(team=self.team), label))
        if self.team:
            self.discussion(speeches)
        plan_label = "第 1 局策略规划（含首日出价）"
        return self._submission_round(plan_label, lambda p, received: p.plan_strategy(
            prompts.opening_plan_request(self._others(speeches, p.name), self.game, received=received, team=self.team),
            plan_label))

    def discussion(self, speeches: dict[str, str]) -> None:
        """团队赛的开局讨论；第 1 轮列出开局发言，每轮收到上一环节写的信。"""
        for number in range(1, DISCUSSION_ROUNDS + 1):
            label = f"开局讨论第 {number} 轮"
            self._run_round(label, lambda p, received: p.discuss(prompts.discussion_request(
                number, others_speeches=self._others(speeches, p.name) if number == 1 else None, received=received),
                label, first=number == 1))

    def match_end(self, logs: list[MatchLog], standings: list[StandingsEntry]) -> dict[str, Bid]:
        """还有下一局时的局末发言、整理记忆和下一局规划，返回规划交出的第 1 天出价；logs 是已结束的全部局。"""
        log = logs[-1]
        index = log.match_index
        label = f"第 {index} 局局末发言"
        speeches = self._speech_round(label, lambda p, received: p.give_speech(
            prompts.match_end_speech_request(log, standings, received=received), label))
        memory_label = f"第 {index} 局累计私有记忆"
        self._run_round(memory_label, lambda p, received: p.save_memory(
            prompts.memory_request(self._others(speeches, p.name), index, received=received, team=self.team),
            memory_label))
        plan_label = f"第 {index + 1} 局策略规划（含首日出价）"
        # 整理记忆不写信，这一轮收不到信。
        return self._submission_round(plan_label, lambda p, _: p.plan_strategy(
            prompts.next_match_plan_request(logs, standings, self.game), plan_label))

    def final(self, standings: list[StandingsEntry], log: MatchLog) -> None:
        """最后一局之后的最终感言和观众总结；最后一局的出价日志随感言请求发出。"""
        label = "最终感言"
        speeches = self._speech_round(label, lambda p, received: p.give_speech(
            prompts.final_speech_request(standings, log, received=received), label, letters=False))
        summary_label = "观众总结（仅观众可见）"
        summaries = self._run_round(summary_label, lambda p, _: p.final_summary(
            prompts.final_summary_request(self._others(speeches, p.name)), summary_label))
        publish_speeches(self.paths, summary_label, {name: output.text for name, output in summaries.items()})
