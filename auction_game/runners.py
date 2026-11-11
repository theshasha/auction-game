"""加载比赛配置、组装 AI 玩家并启动个人赛或团队赛；续跑时按场次快照和任务日志接着跑。"""

from __future__ import annotations

from dataclasses import asdict, fields
from pathlib import Path

from auction_game.flow.director import AIDirector
from auction_game.agent.log import AgentLog, ResumeError
from auction_game.agent.player import AIPlayer
from auction_game.agent.prompts import build_system_prompt
from auction_game.agent.text_output import LetterNames, TeamBoard
from auction_game.configuration import ConfigurationError, load_configuration, load_game_settings, load_llm_settings
from auction_game.flow.controller import Controller, TournamentAborted
from auction_game.llm.client import MissingApiKeyError, make_model_factory
from auction_game.agent.tasks import TaskRunner
from auction_game.cli.rules import sync_root_rules
from auction_game.game.lineup import Lineup, PlayerProfile
from auction_game.game.rules import render_rules
from auction_game.game.settings import GameSettings
from auction_game.paths import CONFIG_PATH, ROOT_DIR
from auction_game.reporting.run_output import (find_unfinished_run, initialize_run, is_finished, make_run_dir,
                                               print_final_result)
from auction_game.reporting.run_paths import OFFLINE_RUN_TYPES, RUN_TYPES, RunPaths

# 场次快照里实际调用的模型只记这些字段，来源和推理档位已在 capabilities 里。
EXECUTION_SNAPSHOT_FIELDS = ("key", "model", "provider", "protocol", "max_tokens", "model_settings", "capabilities")


def execution_calls(player_configs) -> dict[str, dict]:
    """玩家到实际调用的模型参数，就是写进场次快照的 execution_model。"""
    return {pc.profile.name: {field: getattr(pc.execution, field) for field in EXECUTION_SNAPSHOT_FIELDS}
            for pc in player_configs}


def make_task_runners(llm: dict, calls: dict[str, dict], model_factory, runner=TaskRunner, signals=None) -> dict:
    """为每名玩家创建任务入口；缺密钥在这里报错，这时新开的还没创建场次目录，续跑的还没改场次文件。

    llm 是 config.yaml 的 llm 段；calls 是玩家到调用参数，新开时来自配置，续跑时来自场次快照。
    """
    return {name: runner(model_factory(
        provider=call["provider"],
        channel=llm["channels"][call["provider"]],
        model=call["model"],
        timeout=llm["timeout_seconds"],
        protocol=call["protocol"],
        max_tokens=call["max_tokens"],
        model_settings=call["model_settings"],
        signals=signals,
    )) for name, call in calls.items()}


def run_ai(test_model: bool = False, team: bool = False) -> int:
    """真实模型比赛，team 时是团队赛；配置、规则或密钥有误时在创建场次目录前停止。"""
    try:
        config, player_configs = load_configuration(test_model=test_model, team=team)
        game = load_game_settings(test_model=test_model)
        sync_root_rules(load_game_settings(), team=team)
        # 根规则按生产参数生成；测试模式的覆盖参数只用于本场，要单独生成一次。
        rules = render_rules(game, Lineup(tuple(pc.profile for pc in player_configs)))
        controller = Controller(mode="step", max_concurrency=config["llm"]["max_concurrency"])
        task_runners = make_task_runners(config["llm"], execution_calls(player_configs), make_model_factory,
                                         signals=controller.signals)
    except (ConfigurationError, MissingApiKeyError) as exc:
        print(f"[配置错误] {exc}")
        return 1
    run_type = ("team" if team else "ai") + ("-test" if test_model else "")
    run_dir = make_run_dir(run_type)
    if test_model:
        test_key = config["test_model"]
        print(f"测试模型：{test_key} / {config['models'][test_key].model}（全员使用，调用真实服务，可能收费）")
        print("角色身份与独立历史保持不变。")
    else:
        print("常规阵容（调用真实服务，可能收费）：")
        for pc in player_configs:
            print(f"  {pc.profile.name}：{pc.execution.model}")
    warned = {pc.execution.key: pc.execution for pc in player_configs
              if pc.execution.capabilities["output_fallback_reason"]}
    for model in warned.values():
        label = "测试模型" if test_model else model.model
        print(f"[预算提醒] {label}：{model.capabilities['output_fallback_reason']}；暂用 {model.max_tokens} tokens。")
    if warned:
        print("请运行 python -m auction_game.cli.sync_models --write 更新模型能力信息；"
              "仍有提醒时检查 configs/models.yaml 的 models_dev 来源。")
    return play_ai(config, player_configs, game, task_runners, controller,
                   run_dir=run_dir, run_type=run_type, test_model=test_model, rules=rules, start_reader=True)


def play_ai(config, player_configs, game, task_runners, controller, *,
            run_dir, run_type, test_model, rules, start_reader=False) -> int:
    """写场次快照并跑完整场 AI 比赛；离线演示传入假模型的任务入口和不交互的控制器。

    rules 是按本场参数生成的规则；入口在创建场次目录前生成它，参数有误时在那里报配置错误。
    """
    lineup = Lineup(tuple(pc.profile for pc in player_configs))
    calls = execution_calls(player_configs)
    # 本场配置快照，统计和展示只读它，续跑按它重建阵容和模型。
    snapshot = {
        "run_type": run_type,
        "mode": "test_model" if test_model else "regular",
        "game": game.to_dict(),
        "max_concurrency": controller.max_concurrency,
        "prompts": dict(config["prompts"]),
        "players": [{
            **asdict(pc.profile),
            "identity_model": pc.identity.to_dict(),
            "execution_model": calls[pc.profile.name],
        } for pc in player_configs],
    }
    paths = initialize_run(run_dir, snapshot, rules)

    players = [
        _make_player(
            profile, lineup, game, task_runners[profile.name],
            AgentLog(profile.name, paths.player(number), readable_path=paths.player(number, readable=True)),
            build_system_prompt(
                profile, lineup, game,
                enable_predictions=config["prompts"]["enable_predictions"],
                enable_personas=config["prompts"]["enable_personas"],
                writing_style=config["writing_style"],
            ))
        for number, profile in enumerate(lineup.profiles, 1)
    ]
    print(f"并发上限：{controller.max_concurrency}")
    prompt_options = config["prompts"]
    if lineup.teams:
        predictions = "不用于团队赛"
    else:
        predictions = "开启" if prompt_options["enable_predictions"] else "关闭"
    print(f"提示设置：赛前预测{predictions}，"
          f"人设{'开启' if prompt_options['enable_personas'] else '关闭'}，"
          f"写作要求{'开启' if prompt_options['enable_writing_style'] else '关闭'}")
    return _play(players, controller, run_dir, game, lineup, resuming=False, start_reader=start_reader)


def resume_ai(run_dir: Path | None = None) -> int:
    """接着跑一场没打完的真实模型比赛；run_dir 没给时取 logs/ 下最新一场没打完的。

    参数、阵容和每个席位调用的模型取自场次快照，渠道地址、超时、并发和密钥取自当前配置。
    根目录的规则反映当前配置，和这一场无关，续跑不生成。
    """
    try:
        if run_dir is None:
            run_dir = find_unfinished_run()
            if run_dir is None:
                raise ResumeError("logs/ 下没有没打完的真实 AI 场次。")
        paths = RunPaths(run_dir)
        if not run_dir.is_dir():
            raise ResumeError(f"没有这个场次目录：{run_dir}")
        if not paths.config.exists():
            raise ResumeError(f"{run_dir} 还没开始，请重新开一场。")
        snapshot = paths.read_config()
        if snapshot["run_type"] in OFFLINE_RUN_TYPES:
            raise ResumeError(f"{run_dir} 是离线赛，续跑会改成调用真实模型。")
        if is_finished(paths):
            raise ResumeError(f"{run_dir} 已经打完。")
        llm = load_llm_settings()
        calls = {pc["name"]: pc["execution_model"] for pc in snapshot["players"]}
        for call in calls.values():
            if call["provider"] not in llm["channels"]:
                raise ConfigurationError(f"{ROOT_DIR / CONFIG_PATH}：llm.channels 里没有本场用到的渠道 "
                                         f"{call['provider']!r}")
        controller = Controller(mode="step", max_concurrency=llm["max_concurrency"])
        task_runners = make_task_runners(llm, calls, make_model_factory, signals=controller.signals)
    except (ConfigurationError, MissingApiKeyError) as exc:
        print(f"[配置错误] {exc}")
        return 1
    except ResumeError as exc:
        print(f"[无法续跑] {exc}")
        return 1
    print(f"续跑：{run_dir}（{RUN_TYPES[snapshot['run_type']]}，调用真实服务，可能收费）")
    return continue_run(run_dir, task_runners, controller, start_reader=True)


def continue_run(run_dir: Path, task_runners: dict, controller, *, start_reader: bool = False) -> int:
    """按场次快照和任务日志组装玩家，接着跑完这一场；task_runners 是玩家到任务入口。"""
    paths = RunPaths(run_dir)
    snapshot = paths.read_config()
    game = GameSettings.from_dict(snapshot["game"])
    profile_fields = [field.name for field in fields(PlayerProfile)]
    lineup = Lineup(tuple(PlayerProfile(**{field: pc[field] for field in profile_fields})
                          for pc in snapshot["players"]))
    try:
        logs = [AgentLog.open(profile.name, paths.player(number), readable_path=paths.player(number, readable=True))
                for number, profile in enumerate(lineup.profiles, 1)]
    except ResumeError as exc:
        print(f"[无法续跑] {exc}")
        return 1
    # 系统提示用日志里的：中途改过人设或写作要求，前后半场仍是同一份。
    players = [_make_player(profile, lineup, game, task_runners[profile.name], log, log.system_text)
               for profile, log in zip(lineup.profiles, logs)]
    print(f"并发上限：{controller.max_concurrency}")
    return _play(players, controller, run_dir, game, lineup, resuming=True, start_reader=start_reader)


def _make_player(profile: PlayerProfile, lineup: Lineup, game: GameSettings, task_runner, log: AgentLog,
                 system_prompt: str) -> AIPlayer:
    return AIPlayer(
        name=profile.name,
        system_prompt=system_prompt,
        letter_scope=TeamBoard() if lineup.teams else LetterNames(profile.name, lineup.others(profile.name)),
        task_runner=task_runner,
        log=log,
        game=game,
    )


def _play(players: list[AIPlayer], controller, run_dir: Path, game: GameSettings, lineup: Lineup, *,
          resuming: bool, start_reader: bool) -> int:
    if start_reader:
        controller.start_stdin_reader()
        print("操作：回车=下一轮 / c=连续 / s=单步 / p=暂停 / q=退出比赛")

    try:
        result = AIDirector(players, controller, run_dir, game=game, team_of=lineup.team_of, resuming=resuming).run()
    except TournamentAborted:
        print(f'\n已退出比赛。续跑：python main.py --resume "{run_dir}"')
        return 0
    except ResumeError as exc:
        print(f"\n[无法续跑] {exc}")
        return 1
    print_final_result(result)
    return 0
