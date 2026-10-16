"""新场次格式的唯一文件布局；旧目录始终只读且明确拒绝。"""

from dataclasses import dataclass
import json
from pathlib import Path

FORMAT_VERSION = 2
RUN_TYPES = {"ai": "常规真实 AI", "ai-test": "测试模型真实 AI（收费）",
             "bot": "Bot 策略程序", "offline": "离线模拟（无真实模型调用）"}


@dataclass(frozen=True)
class RunPaths:
    root: Path

    @property
    def data(self):
        return self.root / "data"

    @property
    def config(self):
        return self.data / "run_config.json"

    def match(self, index, *, readable=False):
        base = self.root if readable else self.data
        return base / "matches" / f"{index:03d}{'.md' if readable else '.json'}"

    def player(self, number, *, readable=False):
        base = self.root if readable else self.data
        return base / "players" / f"{number:03d}{'.md' if readable else '.jsonl'}"

    def capture(self, number):
        return self.data / "replay" / f"{number:03d}.jsonl"

    def definitions(self, number):
        return self.data / "replay" / f"{number:03d}_definitions.json"

    @property
    def tools(self):
        return self.root / "tools.md"

    @property
    def tool_definitions(self):
        return self.data / "tool_definitions.json"

    @property
    def tool_requests(self):
        return self.data / "tool_requests.jsonl"

    def prepare(self):
        for directory in (self.data / "matches", self.data / "players", self.root / "matches", self.root / "players"):
            directory.mkdir(parents=True, exist_ok=True)

    def read_config(self):
        try:
            config = json.loads(self.config.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ValueError(f"旧格式或无效场次：{self.root} 缺少 data/run_config.json；仅支持新格式") from exc
        if not isinstance(config, dict) or config.get("format_version") != FORMAT_VERSION or config.get("run_type") not in RUN_TYPES:
            raise ValueError("旧格式或不支持的场次版本；仅支持新格式")
        if not isinstance(config.get("game"), dict) or config.get("mode") not in ("regular", "test_model"):
            raise ValueError("场次缺少有效比赛参数或配置模式")
        players = config.get("players", [])
        if (not isinstance(players, list) or not players or any(not isinstance(pc, dict)
                or not isinstance(pc.get("name"), str) or not pc["name"].strip() for pc in players)
                or [pc.get("number") for pc in players] != list(range(1, len(players) + 1))
                or len({pc.get("name") for pc in players}) != len(players)):
            raise ValueError("场次玩家编号或名字无效")
        return config

    def number(self, name):
        for pc in self.read_config()["players"]:
            if pc["name"] == name:
                return pc["number"]
        raise ValueError(f"场次没有玩家：{name}")
