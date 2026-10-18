"""新场次格式的唯一文件布局；旧目录始终只读且明确拒绝。"""

from dataclasses import dataclass
import json
import math
from pathlib import Path
from urllib.parse import urlsplit

from auction_game.configuration import PROTOCOLS
from auction_game.game.game_rules import GameSettings

FORMAT_VERSION = 3
RUN_TYPES = {"ai": "常规真实 AI", "ai-test": "测试模型真实 AI（收费）",
             "bot": "Bot 策略程序", "offline": "离线模拟（无真实模型调用）"}


def validate_run_config(config):
    """读写共用的完整快照校验；历史参数绝不从当前配置补值。"""
    if (not isinstance(config, dict) or type(config.get("format_version")) is not int
            or config["format_version"] != FORMAT_VERSION
            or not isinstance(config.get("run_type"), str) or config["run_type"] not in RUN_TYPES):
        raise ValueError("旧格式或不支持的场次版本；仅支持格式版本 3，历史日志原地保留")
    if config.get("mode") not in ("regular", "test_model"):
        raise ValueError("场次缺少有效配置模式")
    try:
        GameSettings.from_dict(config.get("game"))
    except ValueError as exc:
        raise ValueError(f"场次 game 参数无效：{exc}") from exc
    players = config.get("players", [])
    if (not isinstance(players, list) or not players or any(not isinstance(pc, dict)
            or not isinstance(pc.get("name"), str) or not pc["name"].strip()
            or type(pc.get("number")) is not int for pc in players)
            or [pc.get("number") for pc in players] != list(range(1, len(players) + 1))
            or len({pc.get("name") for pc in players}) != len(players)):
        raise ValueError("场次玩家编号或名字无效")
    # Bot 无模型来源；其他类型须保留分析和离线回放实际读取的字段。
    if config["run_type"] != "bot":
        timeout = config.get("timeout_seconds")
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0
                or (isinstance(timeout, float) and not math.isfinite(timeout))):
            raise ValueError("场次 timeout_seconds 必须是大于 0 的有限数字")
        for pc in players:
            field = f"场次玩家 {pc['number']}"
            identity = pc.get("identity_model")
            if (not isinstance(identity, dict) or not isinstance(identity.get("model"), str)
                    or not identity["model"].strip()):
                raise ValueError(f"{field} identity_model.model 必须是非空字符串")
            execution = pc.get("execution_model")
            if not isinstance(execution, dict):
                raise ValueError(f"{field} execution_model 必须是模型来源快照")
            if execution.get("protocol") not in PROTOCOLS:
                raise ValueError(f"{field} execution_model.protocol 必须是 {' / '.join(PROTOCOLS)}")
            model = execution.get("model")
            if not isinstance(model, str) or not model.strip():
                raise ValueError(f"{field} execution_model.model 必须是非空字符串")
            url = execution.get("base_url")
            try:
                parsed = urlsplit(url) if isinstance(url, str) else None
                valid_url = (parsed and parsed.scheme in ("http", "https") and parsed.hostname
                             and not parsed.username and not parsed.password and not parsed.query
                             and not parsed.fragment and not any(c.isspace() for c in url))
                if parsed:
                    parsed.port
            except ValueError:
                valid_url = False
            if not valid_url:
                raise ValueError(f"{field} execution_model.base_url 必须是无鉴权或查询参数的 HTTP(S) 地址")
            max_tokens = execution.get("max_tokens")
            if type(max_tokens) is not int or max_tokens <= 0:
                raise ValueError(f"{field} execution_model.max_tokens 必须是正整数")
    return config


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
        return validate_run_config(config)

    def number(self, name):
        for pc in self.read_config()["players"]:
            if pc["name"] == name:
                return pc["number"]
        raise ValueError(f"场次没有玩家：{name}")
