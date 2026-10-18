"""统一解析模型目录与本场玩家；不读取密钥，默认路径固定在项目根目录。"""

from __future__ import annotations

import math
import re
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from auction_game.game.game_rules import GameSettings
from auction_game.model_capabilities import effective_capabilities, read_snapshot
from auction_game.paths import ROOT_DIR, CONFIG_PATH, PLAYERS_PATH, MODELS_PATH, GAME_PATH, CAPABILITIES_PATH
PROTOCOLS = ("chat", "responses", "anthropic")
EXECUTION_FIELDS = ("model", "provider", "protocol", "max_tokens", "model_settings", "model_profile", "capabilities")
PROMPT_FLAGS = ("enable_predictions", "enable_personas")


class ConfigurationError(ValueError):
    """带文件与字段位置的配置错误，供命令行入口直接提示。"""


def _read_mapping(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ConfigurationError(f"{path}：读取失败：{exc}") from exc
    if not isinstance(data, dict):
        raise ConfigurationError(f"{path}：根节点必须是键值配置")
    return data


def load_configuration(
    config_path: str | Path = CONFIG_PATH,
    players_path: str | Path = PLAYERS_PATH,
    models_path: str | Path = MODELS_PATH,
    *, test_model: bool = False, snapshot_path: str | Path | None = None,
) -> tuple[dict, list[dict]]:
    """返回运行设置（含已校验 models 目录）及本场玩家；平铺字段为实际调用配置。

    identity_model 保留身份目录快照；测试模式只替换执行配置，不改变人设或名字。
    路径可显式传入，相对路径以项目根目录为基准；不依赖比赛参数。
    """
    config_path = ROOT_DIR / config_path
    players_path = ROOT_DIR / players_path
    config = _read_mapping(config_path)
    if "players" in config:
        raise ConfigurationError(f"{config_path}：players 请移至 {players_path}")
    prompts = config.setdefault("prompts", {})
    if not isinstance(prompts, dict):
        raise ConfigurationError(f"{config_path}：prompts 必须是键值配置")
    for field in PROMPT_FLAGS:
        if type(prompts.setdefault(field, True)) is not bool:
            raise ConfigurationError(f"{config_path}：prompts.{field} 必须是布尔值")
    llm = config.setdefault("llm", {})
    if not isinstance(llm, dict):
        raise ConfigurationError(f"{config_path}：llm 必须是键值配置")
    max_concurrency = llm.setdefault("max_concurrency", 10)
    if type(max_concurrency) is not int or max_concurrency <= 0:
        raise ConfigurationError(f"{config_path}：llm.max_concurrency 必须是正整数")
    timeout = llm.setdefault("timeout_seconds", 60)
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or timeout <= 0
        or (isinstance(timeout, float) and not math.isfinite(timeout))
    ):
        raise ConfigurationError(f"{config_path}：llm.timeout_seconds 必须是大于 0 的有限数字")

    channels = llm.get("channels")
    if not isinstance(channels, dict) or not channels:
        raise ConfigurationError(f"{config_path}：llm.channels 必须是非空键值配置")
    for name, channel in channels.items():
        field = f"llm.channels.{name}"
        if not isinstance(name, str) or not name.strip() or not isinstance(channel, dict):
            raise ConfigurationError(f"{config_path}：{field} 必须有名称，内容必须是键值配置")
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
            raise ConfigurationError(f"{config_path}：{field}.base_url 必须是 HTTP(S) 地址，不能包含账号、密码或查询参数")
        key = channel.get("api_key_env")
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ConfigurationError(f"{config_path}：{field}.api_key_env 必须是环境变量名")
        for profile_key in ("responses_profile", "chat_profile"):
            if profile_key not in channel:
                continue
            profile = channel[profile_key]
            if (not isinstance(profile, dict) or set(profile) - {"supports_forced_tool_choice"}
                    or any(type(value) is not bool for value in profile.values())):
                raise ConfigurationError(
                    f"{config_path}：{field}.{profile_key} 只支持布尔 supports_forced_tool_choice"
                )

    catalog = load_model_catalog(models_path, snapshot_path=snapshot_path)
    for key, model in catalog.items():
        if model["provider"] not in channels:
            raise ConfigurationError(
                f"{ROOT_DIR / models_path}：models.{key}.provider 未知渠道：{model['provider']!r}"
            )
    test_key = config.get("test_model")
    if not isinstance(test_key, str) or test_key not in catalog:
        raise ConfigurationError(f"{config_path}：test_model 必须引用 models.yaml 中的目录键")
    config["models"] = catalog
    players = load_players(players_path, models_path, catalog=catalog)
    if test_model:
        for player in players:
            execution = catalog[test_key]
            player.update({key: execution[key] for key in EXECUTION_FIELDS})
            player["model_key"] = test_key
    return config, players


def channel_model_profile(channel: dict, protocol: str, model_profile: dict | None = None) -> dict:
    """先取协议的渠道默认，再叠加实际执行模型的覆盖；不修改公共能力快照。"""
    if protocol not in ("chat", "responses"):
        return {}
    return deepcopy({**channel.get(f"{protocol}_profile", {}), **(model_profile or {})})


def load_game_settings(path: str | Path = GAME_PATH) -> GameSettings:
    """读取完整比赛参数；相对路径以项目根目录为基准，错误包含文件及字段。"""
    path = ROOT_DIR / path
    data = _read_mapping(path)
    try:
        return GameSettings.from_dict(data)
    except ValueError as exc:
        raise ConfigurationError(f"{path}：{exc}") from exc


def load_model_catalog(path: str | Path = MODELS_PATH, *, resolve_capabilities=True,
                       snapshot_path: str | Path | None = None) -> dict[str, dict]:
    """读取可选模型；无 name 的条目仅作执行替身，不能作为参赛身份。"""
    path = ROOT_DIR / path
    models = _read_mapping(path).get("models")
    if not isinstance(models, dict) or not models:
        raise ConfigurationError(f"{path}：models 必须是非空键值配置")
    names = set()
    for key, model in models.items():
        field = f"models.{key}"
        if not isinstance(key, str) or not key.strip() or not isinstance(model, dict):
            raise ConfigurationError(f"{path}：{field} 必须有名称，内容必须是键值配置")
        for prop in ("model", "provider"):
            if not isinstance(model.get(prop), str) or not model[prop].strip():
                raise ConfigurationError(f"{path}：{field}.{prop} 必须是非空字符串")
        if model.get("protocol") not in PROTOCOLS:
            raise ConfigurationError(f"{path}：{field}.protocol 必须是 chat / responses / anthropic")
        profile = model.setdefault("model_profile", {})
        if (not isinstance(profile, dict) or set(profile) - {"supports_forced_tool_choice"}
                or any(type(value) is not bool for value in profile.values())):
            raise ConfigurationError(
                f"{path}：{field}.model_profile 只支持布尔 supports_forced_tool_choice"
            )
        if "reasoning_effort" in model:
            if (model["protocol"] not in ("chat", "responses")
                    or model["reasoning_effort"] not in ("low", "medium", "high")):
                raise ConfigurationError(f"{path}：{field}.reasoning_effort 仅支持 chat/responses 的 low / medium / high")
        if "max_tokens" in model:
            raise ConfigurationError(f"{path}：{field}.max_tokens 请移除，预算由能力快照统一生成")
        source = model.get("models_dev")
        if (not isinstance(source, dict) or any(not isinstance(source.get(prop), str)
                or not source[prop].strip() for prop in ("provider", "model"))):
            raise ConfigurationError(f"{path}：{field}.models_dev 必须指定精确 provider/model")
        if "canonical_model_id" in source and (not isinstance(source["canonical_model_id"], str)
                                               or not source["canonical_model_id"].strip()):
            raise ConfigurationError(f"{path}：{field}.models_dev.canonical_model_id 必须是非空字符串")
        if "name" in model:
            name = model["name"]
            if (not isinstance(name, str) or not name.strip()
                    or name != name.strip() or re.search(r'[<>:"/\\|?*\x00-\x1f]', name)
                    or name.endswith(".")):
                raise ConfigurationError(f"{path}：{field}.name 必须是可用于日志文件名的非空角色名")
            if name in names:
                raise ConfigurationError(f"{path}：{field}.name 名字重复：{name!r}")
            names.add(name)
    if resolve_capabilities:
        try:
            snapshot = read_snapshot(ROOT_DIR / (snapshot_path if snapshot_path is not None else CAPABILITIES_PATH))
            for key, model in models.items():
                model.update(effective_capabilities(key, model, snapshot))
        except (OSError, ValueError) as exc:
            raise ConfigurationError(f"{path}：能力快照读取失败：{exc}。"
                                     "请运行 python -m auction_game.cli.sync_models --write 更新快照。") from exc
    return models


def load_players(path: str | Path = PLAYERS_PATH, models_path: str | Path = MODELS_PATH,
                 *, catalog: dict | None = None, resolve_capabilities=True) -> list[dict]:
    """合并身份目录与可变人设，供规则与生产入口共用；不依赖连接或比赛配置。"""
    players_path = ROOT_DIR / path
    catalog = catalog if catalog is not None else load_model_catalog(models_path, resolve_capabilities=resolve_capabilities)
    players = _read_mapping(players_path).get("players")
    if not isinstance(players, list) or not players:
        raise ConfigurationError(f"{players_path}：players 必须是非空列表")
    names = set()
    resolved = []
    for index, player in enumerate(players):
        field = f"players[{index}]"
        if not isinstance(player, dict):
            raise ConfigurationError(f"{players_path}：{field} 必须是键值配置")
        extra = set(player) - {"model", "persona_title", "persona_text", "predicted_win_rate"}
        if extra:
            raise ConfigurationError(f"{players_path}：{field} 不支持字段：{', '.join(sorted(extra))}")
        for key in ("persona_title", "persona_text", "model"):
            value = player.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ConfigurationError(f"{players_path}：{field}.{key} 必须是非空字符串")
        model_key = player["model"]
        if model_key not in catalog:
            raise ConfigurationError(f"{players_path}：{field}.model 未知目录键：{model_key!r}")
        identity = {"key": model_key, **catalog[model_key]}
        name = identity.get("name")
        if not name:
            raise ConfigurationError(f"{players_path}：{field}.model 没有参赛角色名")
        if name in names:
            raise ConfigurationError(f"{players_path}：{field}.name 名字重复：{name!r}")
        names.add(name)
        rate = player.get("predicted_win_rate", 0)
        if (isinstance(rate, bool) or not isinstance(rate, (int, float))
                or not math.isfinite(rate) or not 0 <= rate <= 100):
            raise ConfigurationError(f"{players_path}：{field}.predicted_win_rate 必须是 0~100 的有限数字")
        resolved.append({**player, **catalog[model_key], "model_key": model_key,
                         "identity_model": identity, "predicted_win_rate": rate})
    return resolved
