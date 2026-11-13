"""场次目录的命名和文件布局。"""

from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import re

from auction_game.paths import LOG_DIR

RUN_TYPES = {"ai": "常规真实 AI", "ai-test": "测试模型真实 AI（收费）", "offline": "离线模拟",
             "team": "团队赛常规真实 AI", "team-test": "团队赛测试模型真实 AI（收费）", "team-offline": "团队赛离线模拟"}
# 全员用假模型、没有真实调用的场次类型。
OFFLINE_RUN_TYPES = ("offline", "team-offline")
REAL_RUN_TYPES = tuple(run_type for run_type in RUN_TYPES if run_type not in OFFLINE_RUN_TYPES)
# make_run_dir 起的目录名：时间戳、场次类型，同名时的序号。
RUN_DIR = re.compile(rf"^(\d{{8}}_\d{{6}})_({'|'.join(map(re.escape, RUN_TYPES))})(?:_(\d+))?$")


def run_type(*, team: bool, test_model: bool = False, offline: bool = False) -> str:
    """场次快照里的 run_type，也是目录名里的类型；离线赛不分测试模式。"""
    if offline:
        return "team-offline" if team else "offline"
    return ("team" if team else "ai") + ("-test" if test_model else "")


def make_run_dir(run_type: str) -> Path:
    """为本场运行创建独立日志目录 logs/<时间戳>_<类型>/，同名时加后缀。"""
    base_name = f"{datetime.now():%Y%m%d_%H%M%S}_{run_type}"
    run_dir = LOG_DIR / base_name
    suffix = 2
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            run_dir.mkdir()
            break
        except FileExistsError:
            run_dir = LOG_DIR / f"{base_name}_{suffix}"
            suffix += 1

    print(f"本场日志目录：{run_dir}")
    return run_dir


def find_runs(run_types: tuple[str, ...]) -> list[Path]:
    """logs/ 下这几种类型的场次目录，从旧到新；archive 等归档目录不算。"""
    runs = []
    for path in LOG_DIR.iterdir() if LOG_DIR.exists() else ():
        match = RUN_DIR.match(path.name)
        if match and match[2] in run_types:
            runs.append(((match[1], int(match[3] or 1)), path))
    return [path for _, path in sorted(runs)]


@dataclass(frozen=True)
class RunPaths:
    root: Path

    @property
    def data(self):
        return self.root / "data"

    @property
    def config(self):
        return self.data / "run_config.json"

    @property
    def matches_report(self):
        return self.root / "matches.md"

    @property
    def speeches(self):
        return self.root / "speeches.md"

    @property
    def letters(self):
        return self.root / "letters.md"

    def match(self, index):
        return self.data / "matches" / f"{index:03d}.json"

    def player(self, number, *, readable=False):
        base = self.root if readable else self.data
        return base / "players" / f"{number:03d}{'.md' if readable else '.jsonl'}"

    def prepare(self):
        for directory in (self.data / "matches", self.data / "players", self.root / "players"):
            directory.mkdir(parents=True, exist_ok=True)

    def read_config(self):
        return json.loads(self.config.read_text(encoding="utf-8"))
