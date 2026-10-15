"""读取运行设置与玩家清单；默认路径固定在项目根目录，不依赖工作目录。"""

from __future__ import annotations

import math
from pathlib import Path

import yaml

ROOT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = ROOT_DIR / "config.yaml"
PLAYERS_PATH = ROOT_DIR / "players.yaml"


class ConfigurationError(ValueError):
    """带文件与字段位置的配置错误，供命令行入口直接提示。"""


def _read_mapping(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ConfigurationError(f"{path}：读取失败：{exc}") from exc
    if not isinstance(data, dict):
        raise ConfigurationError(f"{path}：根节点必须是 mapping（键值配置）")
    return data


def load_configuration(
    config_path: str | Path = CONFIG_PATH,
    players_path: str | Path = PLAYERS_PATH,
) -> tuple[dict, list[dict]]:
    """返回 (运行设置, 玩家列表)，补入 timeout=60 与预测夺冠率=0 的缺省值。

    两个路径均可显式传入；相对路径以本模块所在的项目根目录为基准。
    provider/协议是否受支持仍由 make_chat_fn 判断。
    """
    config_path = ROOT_DIR / config_path
    players_path = ROOT_DIR / players_path
    config = _read_mapping(config_path)
    if "players" in config:
        raise ConfigurationError(f"{config_path}：players 请移至 {players_path}")
    llm = config.setdefault("llm", {})
    if not isinstance(llm, dict):
        raise ConfigurationError(f"{config_path}：llm 必须是 mapping（键值配置）")
    timeout = llm.setdefault("timeout_seconds", 60)
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or timeout <= 0
        or (isinstance(timeout, float) and not math.isfinite(timeout))
    ):
        raise ConfigurationError(f"{config_path}：llm.timeout_seconds 必须是正有限数字")

    players = _read_mapping(players_path).get("players")
    if not isinstance(players, list) or not players:
        raise ConfigurationError(f"{players_path}：players 必须是非空列表")
    names = set()
    for index, player in enumerate(players):
        field = f"players[{index}]"
        if not isinstance(player, dict):
            raise ConfigurationError(f"{players_path}：{field} 必须是 mapping（键值配置）")
        for key in ("name", "persona_title", "persona_text", "provider", "model"):
            value = player.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ConfigurationError(f"{players_path}：{field}.{key} 必须是非空字符串")
        name = player["name"]
        if name in names:
            raise ConfigurationError(f"{players_path}：{field}.name 名字重复：{name!r}")
        names.add(name)
        protocol = player.get("protocol")
        if protocol is not None and (not isinstance(protocol, str) or not protocol.strip()):
            raise ConfigurationError(f"{players_path}：{field}.protocol 必须是非空字符串或 null")
        player.setdefault("predicted_win_rate", 0)
    return config, players
