"""解析渠道、模型目录和本场玩家；相对路径以项目根目录为基准，密钥另由模型工厂读取。"""

from __future__ import annotations

from pathlib import Path

import yaml

from auction_game.ai.text_output import FIXED_TAGS
from auction_game.game.game_rules import GameSettings
from auction_game.model_capabilities import effective_capabilities, read_snapshot
from auction_game.paths import (ROOT_DIR, CONFIG_PATH, PLAYERS_PATH, MODELS_PATH, GAME_PATH, CAPABILITIES_PATH,
                                WRITING_STYLE_PATH)
PROTOCOLS = ("chat", "responses", "anthropic")
EXECUTION_FIELDS = ("model", "provider", "protocol", "max_tokens", "model_settings", "capabilities")
PROMPT_FLAGS = ("enable_predictions", "enable_personas", "enable_writing_style")


class ConfigurationError(ValueError):
    """带文件和字段位置的配置错误，命令行入口直接打印。"""


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
    """返回运行设置（含 models 目录）和本场玩家。

    玩家的平铺字段是实际调用配置，identity_model 是身份所在的目录条目；
    测试模式只替换执行配置，名字和人设不变。
    """
    config_path = ROOT_DIR / config_path
    players_path = ROOT_DIR / players_path
    config = _read_mapping(config_path)
    prompts = config.setdefault("prompts", {})
    if not isinstance(prompts, dict):
        raise ConfigurationError(f"{config_path}：prompts 必须是键值配置")
    for field in PROMPT_FLAGS:
        if type(prompts.setdefault(field, True)) is not bool:
            raise ConfigurationError(f"{config_path}：prompts.{field} 必须是布尔值")
    config["writing_style"] = None
    if prompts["enable_writing_style"]:
        try:
            config["writing_style"] = WRITING_STYLE_PATH.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ConfigurationError(f"{WRITING_STYLE_PATH}：写作要求读取失败：{exc}") from exc
    llm = config.setdefault("llm", {})
    if not isinstance(llm, dict):
        raise ConfigurationError(f"{config_path}：llm 必须是键值配置")
    max_concurrency = llm.setdefault("max_concurrency", 10)
    if type(max_concurrency) is not int or max_concurrency <= 0:
        raise ConfigurationError(f"{config_path}：llm.max_concurrency 必须是正整数")
    timeout = llm.setdefault("timeout_seconds", 60)
    if type(timeout) not in (int, float) or not timeout > 0:
        raise ConfigurationError(f"{config_path}：llm.timeout_seconds 必须是大于 0 的数字")

    channels = llm.get("channels")
    if not isinstance(channels, dict) or not channels:
        raise ConfigurationError(f"{config_path}：llm.channels 必须是非空键值配置")
    for name, channel in channels.items():
        field = f"llm.channels.{name}"
        if not isinstance(channel, dict):
            raise ConfigurationError(f"{config_path}：{field} 必须是键值配置")
        url = channel.get("base_url")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise ConfigurationError(f"{config_path}：{field}.base_url 必须是 HTTP(S) 地址")
        if not isinstance(channel.get("api_key_env"), str):
            raise ConfigurationError(f"{config_path}：{field}.api_key_env 必须是环境变量名")

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
        execution = catalog[test_key]
        for player in players:
            player.update({key: execution[key] for key in EXECUTION_FIELDS})
            player["model_key"] = test_key
    return config, players


def load_game_settings(path: str | Path = GAME_PATH, *, test_model: bool = False) -> GameSettings:
    """读取比赛参数；test_model 时用 test_model_overrides 覆盖同名字段。报错带文件路径。"""
    path = ROOT_DIR / path
    data = _read_mapping(path)
    overrides = data.pop("test_model_overrides", {})
    if not isinstance(overrides, dict):
        raise ConfigurationError(f"{path}：test_model_overrides 必须是键值配置")
    if test_model:
        data.update(overrides)
    try:
        return GameSettings.from_dict(data)
    except (ValueError, TypeError) as exc:
        raise ConfigurationError(f"{path}：{exc}") from exc


def load_model_catalog(path: str | Path = MODELS_PATH, *, resolve_capabilities=True,
                       snapshot_path: str | Path | None = None) -> dict[str, dict]:
    """读取模型目录；name 是条目参赛时默认的角色名。"""
    path = ROOT_DIR / path
    models = _read_mapping(path).get("models")
    if not isinstance(models, dict) or not models:
        raise ConfigurationError(f"{path}：models 必须是非空键值配置")
    for key, model in models.items():
        field = f"models.{key}"
        if not isinstance(model, dict):
            raise ConfigurationError(f"{path}：{field} 必须是键值配置")
        for prop in ("model", "provider"):
            if not isinstance(model.get(prop), str) or not model[prop].strip():
                raise ConfigurationError(f"{path}：{field}.{prop} 必须是非空字符串")
        if model.get("protocol") not in PROTOCOLS:
            raise ConfigurationError(f"{path}：{field}.protocol 必须是 chat / responses / anthropic")
        if "reasoning_effort" in model and model["reasoning_effort"] not in ("low", "medium", "high"):
            raise ConfigurationError(f"{path}：{field}.reasoning_effort 仅支持 low / medium / high")
        source = model.get("models_dev")
        if (not isinstance(source, dict) or any(not isinstance(source.get(prop), str)
                or not source[prop].strip() for prop in ("provider", "model"))):
            raise ConfigurationError(f"{path}：{field}.models_dev 必须指定精确 provider/model")
        if "name" in model and (not isinstance(model["name"], str) or not model["name"].strip()):
            raise ConfigurationError(f"{path}：{field}.name 必须是非空字符串")
    if resolve_capabilities:
        try:
            snapshot = read_snapshot(ROOT_DIR / (snapshot_path if snapshot_path is not None else CAPABILITIES_PATH))
        except (OSError, ValueError) as exc:
            raise ConfigurationError(f"{path}：能力快照读取失败：{exc}。"
                                     "请运行 python -m auction_game.cli.sync_models --write 更新快照。") from exc
        for key, model in models.items():
            model.update(effective_capabilities(key, model, snapshot))
    return models


def load_players(path: str | Path = PLAYERS_PATH, models_path: str | Path = MODELS_PATH,
                 *, catalog: dict | None = None, resolve_capabilities=True) -> list[dict]:
    """合并身份目录和人设，得到本场玩家；规则生成和比赛入口共用。"""
    players_path = ROOT_DIR / path
    catalog = catalog if catalog is not None else load_model_catalog(models_path, resolve_capabilities=resolve_capabilities)
    players = _read_mapping(players_path).get("players")
    if not isinstance(players, list) or len(players) < 2:
        raise ConfigurationError(f"{players_path}：players 必须是至少两名玩家的列表")
    names = set()
    resolved = []
    for index, player in enumerate(players):
        field = f"players[{index}]"
        if not isinstance(player, dict):
            raise ConfigurationError(f"{players_path}：{field} 必须是键值配置")
        extra = set(player) - {"name", "model", "persona_title", "persona_text", "predicted_win_rate"}
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
        name = _player_name(players_path, field, player, model_key, identity.get("name"))
        if name in names:
            raise ConfigurationError(f"{players_path}：{field} 名字重复：{name!r}；"
                                     "同一个模型放多名玩家时，给每人写 name")
        names.add(name)
        rate = player.get("predicted_win_rate", 0)
        if type(rate) not in (int, float) or not 0 <= rate <= 100:
            raise ConfigurationError(f"{players_path}：{field}.predicted_win_rate 必须是 0~100 的数字")
        resolved.append({**player, **catalog[model_key], "name": name, "model_key": model_key,
                         "identity_model": identity, "predicted_win_rate": rate})
    return resolved


def _player_name(players_path, field, player, model_key, catalog_name) -> str:
    """阵容写了 name 就用它，否则用目录里的角色名。限制的原因见 docs/letters.md 的"名字"。"""
    if "name" in player:
        name = player["name"]
        if not isinstance(name, str) or not name:
            raise ConfigurationError(f"{players_path}：{field}.name 必须是非空字符串")
        label = f"{field}.name "
    elif catalog_name is None:
        raise ConfigurationError(f"{players_path}：{field} 没有名字：在这里写 name，或给 models.{model_key} 加 name")
    else:
        name = catalog_name
        label = f"{field} 的名字取自 models.{model_key}.name，"
    if any(char.isspace() or char in "<>/" for char in name):
        raise ConfigurationError(f"{players_path}：{label}不能含空白和 <、>、/：{name!r}")
    if name in FIXED_TAGS:
        raise ConfigurationError(f"{players_path}：{label}不能是 {'、'.join(FIXED_TAGS)}：{name!r}")
    return name
