"""整场 AI 比赛的流程：开局发言、团队赛每局的开局讨论、规划、每天出价、局末发言、整理记忆和终局；信在收件人的下一轮送达。

续跑时从头重走同一套流程：日志里已有成功记录的任务直接取用，结算、信箱和排名随流程重新算出。
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from auction_game.agent import prompts
from auction_game.agent.log import ResumeError
from auction_game.agent.player import AIPlayer
from auction_game.agent.tasks import TaskOutput
from auction_game.agent.text_output import BOARD
from auction_game.flow.controller import Controller, RoundTask
from auction_game.game.engine import MatchState, compute_standings
from auction_game.game.settings import DISCUSSION_ROUNDS, GameSettings
from auction_game.game.models import MatchLog, Observation, StandingsEntry
from auction_game.game.tournament import TournamentResult, has_next_match
from auction_game.reporting.run_output import (SUMMARY_TITLE, print_day_result, publish_match, publish_speeches,
                                               record_letters, reset_reports, write_readme)
from auction_game.reporting.run_paths import RunPaths

Bid = tuple[int, int | None]  # (正常出价, 超级出价或 None)


class AIDirector:
    """每个环节由全员并发完成一轮任务，经过 controller 的单步与暂停控制。"""

    def __init__(self, players: list[AIPlayer], controller: Controller, log_dir: Path | None, *, game: GameSettings,
                 team_of: dict[str, str] | None = None, resuming: bool = False,
                 new_run_dir: Callable[[], Path] | None = None, write_new_run: Callable[[Path], None] | None = None):
        """team_of 是团队赛的玩家到阵营，交给结算和排名；个人赛为 None。resuming 时玩家带着已有日志，先重放。

        新开的场次 log_dir 为 None，第一轮放行后先调用 new_run_dir 建场次目录，再调用 write_new_run 往里写快照、
        规则和玩家日志；放行前按 q 退出，不建场次目录。files_ready 在场次文件写全后为真，续跑的场次一开始就是。
        """
        self.players = players
        self.controller = controller
        self.paths = None if log_dir is None else RunPaths(log_dir)
        self.game = game
        self.team_of = team_of
        self.team = team_of is not None
        # 待送达的信：收件人到 {写信人: 正文}，写信人按配置顺序。
        self.mailbox: dict[str, dict[str, str]] = {}
        self._new_run_dir = new_run_dir
        self._write_new_run = write_new_run
        self._resuming = resuming
        self.files_ready = new_run_dir is None
        # 第一轮放行前记下的公开文件写入，放行后按顺序补写；之后为 None。
        self._deferred: list[Callable[[bool], None]] | None = [] if resuming or new_run_dir else None

    def run(self) -> TournamentResult:
        """按顺序跑完整场比赛，返回最终结果。

        每局第 1 天用规划交出的出价结算，第 2 天起每天一轮出价；每局结束先公布结果，还有下一局时
        再做局末发言、整理记忆、团队赛的开局讨论和下一局规划，最后一局之后是最终感言和观众总结。
        """
        names = [player.name for player in self.players]
        logs: list[MatchLog] = []
        # 玩家建好时写下了私人对话，README 从开场就链到它们。
        self._report(lambda quiet: write_readme(self.paths))
        day1_bids = self.opening()
        while True:
            match = MatchState(names, len(logs) + 1, len(logs) >= self.game.regular_matches, logs, self.game,
                               team_of=self.team_of)
            for day in range(1, self.game.max_days + 1):
                observations = match.start_day()
                match.settle(day1_bids if day == 1 else self.bid_round(observations))
                if self._deferred is None:
                    print_day_result(match.log, match.standings())
            logs.append(match.log)
            standings = compute_standings(names, logs, team_of=self.team_of)
            self._report(lambda quiet, log=match.log, standings=standings: publish_match(
                self.paths, log, standings, quiet=quiet))
            if not has_next_match(logs, standings, self.game):
                break
            day1_bids = self.match_end(logs, standings)
        self.final(standings, logs[-1])
        self._open_files()  # 最后一轮全员成功、还没发布就被关掉的场次，重放一直走到这里。
        return TournamentResult(logs, standings)

    def _report(self, write: Callable[[bool], None]) -> None:
        """write(quiet) 写公开文件并打印。第一轮放行前先记下：新开的还没有场次目录，续跑的不动已有文件。"""
        if self._deferred is None:
            write(False)
        else:
            self._deferred.append(write)

    def _open_files(self) -> None:
        """第一轮放行后调用一次，把场次文件写到这一轮开始时的样子，补写时不打印。

        新开的场次先建目录，记下位置后再写快照和玩家日志，写到一半出错时调用方能说出目录在哪。
        续跑的记录都已核对通过，截掉任务日志的半行，重写私人对话，公开发言、信和逐局结果清空后按记下的顺序补写。
        """
        if self._deferred is None:
            return
        if self._new_run_dir is not None:
            self.paths = RunPaths(self._new_run_dir())
            self._write_new_run(self.paths.root)
            self.files_ready = True
        else:
            for player in self.players:
                player.log.restore_files()
            reset_reports(self.paths)
        for write in self._deferred:
            write(True)
        self._deferred = None

    def _run_round(self, label: str, run: Callable[[AIPlayer, dict[str, str]], TaskOutput]) -> dict[str, TaskOutput]:
        """全员执行 run(玩家, 收到的信)；按配置中的玩家顺序返回各人的输出。

        label 是这一轮的标题，也是私人日志、失败公告和 letters.md 的节标题。收到的信在轮开始前取出，补跑时不变。
        controller.run_round 只在整轮成功并确认后返回，这时才记下这一轮写的信、再用它们替换邮箱，
        所以退出时邮箱仍是轮开始时的状态。

        续跑时，下一条记录正是这一轮的玩家直接取用，其余的才交给 controller；整轮都有记录就不经过 controller。
        """
        inbox = self.mailbox
        results: dict[str, TaskOutput] = {}
        tasks = []
        for player in self.players:
            recorded = player.recorded_title()
            if recorded == label:
                results[player.name] = run(player, inbox.get(player.name, {}))
            elif recorded is not None:
                # 发任何请求之前就停下：流程和日志里的轮次顺序对不上。
                raise ResumeError(f"{player.name} 的下一条记录是“{recorded}”，流程走到的是“{label}”。"
                                  "这场是用另一版代码跑的。")
            else:
                tasks.append(RoundTask(key=player.name, participant=player,
                                       fn=lambda player=player: run(player, inbox.get(player.name, {}))))
        if tasks:
            if self._deferred is None:
                results.update(self.controller.run_round(tasks, label))
            else:
                # 放行这一轮后才建或改场次文件：放行前按 q，什么都不动。
                if self._resuming:
                    print(f"已恢复到：{label}（{len(self.players)} 人中 {len(results)} 人已完成）")
                results.update(self.controller.run_round(tasks, label, on_start=self._open_files))
        outputs = {player.name: results[player.name] for player in self.players}
        letters = [(sender, letter["to"], letter["text"]) for sender, output in outputs.items()
                   for letter in output.letters]
        # 信只写进 letters.md，不打印到终端：个人赛一天最多 12 封私信，太长。
        # 队内信件区的信沿用私信的数据，to 是固定标签，放进写信人每名队友的邮箱。
        logged = [(sender, self.team_of[sender] if to == BOARD else to, text) for sender, to, text in letters]
        self._report(lambda quiet: record_letters(self.paths, label, logged, team=self.team))
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
        self._report(lambda quiet: publish_speeches(self.paths, label, speeches, quiet=quiet))
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
            self.discussion(1, speeches=speeches)
        plan_label = "第 1 局策略规划（含首日出价）"
        return self._submission_round(plan_label, lambda p, received: p.plan_strategy(
            prompts.opening_plan_request(self._others(speeches, p.name), self.game, received=received, team=self.team),
            plan_label))

    def discussion(self, match_index: int, *, speeches: dict[str, str] | None = None,
                   past_matches: str | None = None) -> None:
        """团队赛一局的开局讨论；第 1 轮列出 speeches（第 1 局）或 past_matches（之后各局），每轮收到上一环节写的信。"""
        for number in range(1, DISCUSSION_ROUNDS + 1):
            first = number == 1
            label = f"第 {match_index} 局开局讨论第 {number} 轮"
            self._run_round(label, lambda p, received: p.discuss(prompts.discussion_request(
                match_index, number, others_speeches=self._others(speeches, p.name) if first and speeches else None,
                past_matches=past_matches if first else None, received=received), label, first=first))

    def match_end(self, logs: list[MatchLog], standings: list[StandingsEntry]) -> dict[str, Bid]:
        """还有下一局时的局末发言、整理记忆、团队赛的开局讨论和下一局规划，返回规划交出的第 1 天出价；logs 是已结束的全部局。"""
        log = logs[-1]
        index = log.match_index
        label = f"第 {index} 局局末发言"
        speeches = self._speech_round(label, lambda p, received: p.give_speech(
            prompts.match_end_speech_request(log, standings, received=received, team=self.team), label))
        memory_label = f"第 {index} 局累计私有记忆"
        self._run_round(memory_label, lambda p, received: p.save_memory(
            prompts.memory_request(self._others(speeches, p.name), index, received=received, team=self.team),
            memory_label))
        plan_label = f"第 {index + 1} 局策略规划（含首日出价）"
        if self.team:
            self.discussion(index + 1, past_matches=prompts.past_matches_info(logs, standings, self.game))
            return self._submission_round(plan_label, lambda p, received: p.plan_strategy(
                prompts.team_next_match_plan_request(index + 1, self.game, received=received), plan_label))
        # 整理记忆不写信，这一轮收不到信。
        return self._submission_round(plan_label, lambda p, _: p.plan_strategy(
            prompts.next_match_plan_request(logs, standings, self.game), plan_label))

    def final(self, standings: list[StandingsEntry], log: MatchLog) -> None:
        """最后一局之后的最终感言和观众总结；最后一局的出价日志随感言请求发出。"""
        label = "最终感言"
        speeches = self._speech_round(label, lambda p, received: p.give_speech(
            prompts.final_speech_request(standings, log, received=received, team=self.team), label, letters=False))
        summaries = self._run_round(SUMMARY_TITLE, lambda p, _: p.final_summary(
            self._others(speeches, p.name), SUMMARY_TITLE))
        texts = {name: output.text for name, output in summaries.items()}
        self._report(lambda quiet: publish_speeches(self.paths, SUMMARY_TITLE, texts, quiet=quiet))
