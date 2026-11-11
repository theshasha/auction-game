"""续跑测试的辅助：让离线假模型赛在指定位置强关，再用同样的假模型接着跑，并比较两场的文件。"""

import json
from pathlib import Path

from pydantic_ai.models import override_allow_model_requests

from auction_game.cli import offline_demo
from auction_game.cli.offline_demo import DemoFactory, OfflineController, OfflineTaskRunner, generate_demo
from auction_game.reporting.run_paths import RunPaths
from auction_game.runners import continue_run


class Killed(Exception):
    """模拟强关：controller 不处理的异常直接传出去，被中断的任务不留记录。"""


class KillSwitch:
    """放行 limit 个任务，之后的任务抛 Killed；limit 为 None 时不拦。每次启动或续跑用一个新的。"""

    def __init__(self, limit=None):
        self.limit, self.started = limit, 0

    def check(self):
        if self.limit is not None and self.started >= self.limit:
            raise Killed
        self.started += 1


class Histories:
    """每次请求带的对话历史。假模型的回复不看历史，所以单独核对：不中断的那场先记下，
    verifying 之后每次请求带的历史都要和记下的相同。"""

    def __init__(self):
        self.seen, self.verifying = {}, False

    def check(self, name, prompt, history):
        content = [[(part.part_kind, part.content) for part in message.parts] for message in history]
        if self.verifying:
            assert content == self.seen[name, prompt], f"{name} 的对话历史和不中断时不同：{prompt[:40]}"
        else:
            assert (name, prompt) not in self.seen
            self.seen[name, prompt] = content


class KillableRunner(OfflineTaskRunner):
    def __init__(self, factory, switch, histories=None):
        super().__init__(factory)
        self.switch, self.histories = switch, histories

    def __call__(self, spec, prompt, history, **kwargs):
        self.switch.check()
        if self.histories is not None:
            self.histories.check(self.factory.name, prompt, history)
        return super().__call__(spec, prompt, history, **kwargs)


def start_demo(monkeypatch, run_dir, switch=None, histories=None, **options):
    """开一场离线假模型赛，任务先过 switch；并发要在配置里设为 1，中断位置才确定。"""
    switch = switch or KillSwitch()
    with monkeypatch.context() as patch:
        patch.setattr(offline_demo, "OfflineTaskRunner", lambda factory: KillableRunner(factory, switch, histories))
        generate_demo(run_dir, **options)


def complete_records(path):
    """玩家 JSONL 里写完整的记录，不含末尾的半行。"""
    data = path.read_bytes()
    return [json.loads(line) for line in data[:data.rfind(b"\n") + 1].decode("utf-8").split("\n") if line.strip()]


def resume_demo(run_dir, switch=None, histories=None):
    """用假模型接着跑一场离线赛，返回 continue_run 的退出码；并发为 1。

    假模型按调用次数推算局、天和信件编号，这里接着日志里的成功记录往下数。
    """
    switch = switch or KillSwitch()
    paths = RunPaths(run_dir)
    players = paths.read_config()["players"]
    runners = {}
    for index, pc in enumerate(players):
        factory = DemoFactory(pc["name"], index, {}, player_count=len(players), team=pc["team"] is not None)
        runner = KillableRunner(factory, switch, histories)
        path = paths.player(pc["number"])
        for entry in complete_records(path) if path.exists() else ():
            if entry.get("status") != "success":
                continue
            if entry["task"] == "plan":
                runner.match_index, runner.day = runner.match_index + 1, 1
            elif entry["task"] == "bid":
                runner.day += 1
            if isinstance(entry["result"], dict):
                factory.letters_written += len(entry["result"].get("letters", ()))
        runners[pc["name"]] = runner
    with override_allow_model_requests(False):
        return continue_run(run_dir, runners, OfflineController(mode="continuous", max_concurrency=1))


def run_files(run_dir):
    """场次目录里每个文件的内容，键是相对路径。

    任务记录只留和运行无关的部分：消息里的时间戳和各种 id 每次运行都不同，只比较各段的类型和文字。
    """
    files = {}
    for path in sorted(Path(run_dir).rglob("*")):
        if not path.is_file():
            continue
        name = path.relative_to(run_dir).as_posix()
        if path.suffix == ".jsonl":
            files[name] = [
                {**record, "messages": [[(part["part_kind"], part.get("content")) for part in message["parts"]]
                                        for message in record["messages"]]} if "messages" in record else record
                for record in complete_records(path)]
        else:
            files[name] = path.read_bytes()
    return files


def assert_same_run(actual_dir, expected_dir):
    actual, expected = run_files(actual_dir), run_files(expected_dir)
    assert list(actual) == list(expected)
    for name in expected:
        assert actual[name] == expected[name], name
