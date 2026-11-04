"""场次文件布局。"""

from dataclasses import dataclass
import json
from pathlib import Path

RUN_TYPES = {"ai": "常规真实 AI", "ai-test": "测试模型真实 AI（收费）", "offline": "离线模拟",
             "team": "团队赛常规真实 AI", "team-test": "团队赛测试模型真实 AI（收费）", "team-offline": "团队赛离线模拟"}
# 全员用假模型、没有真实调用的场次类型。
OFFLINE_RUN_TYPES = ("offline", "team-offline")


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
