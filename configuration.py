"""读取运行设置、比赛参数与玩家清单；默认路径固定在项目根目录。"""

from __future__ import annotations

import math
import re
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from game_rules import GameSettings

ROOT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = ROOT_DIR / "config.yaml"
PLAYERS_PATH = ROOT_DIR / "players.yaml"
GAME_PATH = ROOT_DIR / "game.yaml"
PROTOCOLS = ("chat", "responses", "anthropic")


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
    渠道及玩家调用参数在这里统一校验；不读取密钥，不依赖比赛参数。
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

    channels = llm.get("channels")
    if not isinstance(channels, dict) or not channels:
        raise ConfigurationError(f"{config_path}：llm.channels 必须是非空 mapping（键值配置）")
    for name, channel in channels.items():
        field = f"llm.channels.{name}"
        if not isinstance(name, str) or not name.strip() or not isinstance(channel, dict):
            raise ConfigurationError(f"{config_path}：{field} 必须是具名 mapping（键值配置）")
        url = channel.get("base_url")
        try:
            parsed = urlsplit(url) if isinstance(url, str) else None
            valid = (parsed and parsed.scheme in ("http", "https") and parsed.hostname
                     and not parsed.username and not parsed.password and not parsed.query
                     and not parsed.fragment and not any(c.isspace() for c in url))
            if parsed:
                parsed.port  # 同时检查无效端口。
        except ValueError:
            valid = False
        if not valid:
            raise ConfigurationError(f"{config_path}：{field}.base_url 必须是无鉴权信息的 HTTP(S) 地址")
        key = channel.get("api_key_env")
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ConfigurationError(f"{config_path}：{field}.api_key_env 必须是环境变量名")

    players = load_players(players_path)
    for index, player in enumerate(players):
        if player["provider"] not in channels:
            raise ConfigurationError(
                f"{players_path}：players[{index}].provider 未知渠道：{player['provider']!r}"
            )
    return config, players


def load_game_settings(path: str | Path = GAME_PATH) -> GameSettings:
    """读取完整比赛参数；相对路径以项目根目录为基准，错误包含文件及字段。"""
    path = ROOT_DIR / path
    data = _read_mapping(path)
    try:
        return GameSettings.from_dict(data)
    except ValueError as exc:
        raise ConfigurationError(f"{path}：{exc}") from exc


def load_players(path: str | Path = PLAYERS_PATH) -> list[dict]:
    """读取并校验玩家清单，补入预测夺冠率=0；不依赖运行或比赛配置。"""
    players_path = ROOT_DIR / path
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
        if player.get("protocol") not in PROTOCOLS:
            raise ConfigurationError(f"{players_path}：{field}.protocol 必须是 chat / responses / anthropic")
        budget = player.get("max_tokens")
        if isinstance(budget, bool) or not isinstance(budget, int) or budget <= 0:
            raise ConfigurationError(f"{players_path}：{field}.max_tokens 必须是正整数")
        player.setdefault("predicted_win_rate", 0)
    return players
