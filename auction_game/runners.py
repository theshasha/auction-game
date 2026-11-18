"""加载比赛配置、组装 AI 玩家并启动个人赛或团队赛；续跑时按场次快照和任务日志接着跑。"""

from __future__ import annotations

import traceback
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from auction_game.flow.director import AIDirector
from auction_game.agent.log import AgentLog, ResumeError
from auction_game.agent.player import AIPlayer
from auction_game.agent.prompts import build_system_prompt
from auction_game.agent.text_output import LetterNames, TeamBoard
from auction_game.configuration import (ConfigurationError, PlayerConfig, load_configuration, load_game_settings,
                                        load_llm_settings, load_players, load_teams)
from auction_game.flow.controller import Controller, TournamentAborted
from auction_game.llm.client import MissingEnvError, make_model_factory
from auction_game.agent.tasks import TaskRunner
from auction_game.game.lineup import Lineup, PlayerProfile
from auction_game.game.rules import rules_document
from auction_game.game.settings import GameSettings
from auction_game.paths import CONFIG_PATH, ROOT_DIR, RULES_PATH, TEAM_RULES_PATH
from auction_game.reporting.run_output import (find_unfinished_run, initialize_run, is_finished, model_label,
                                               print_final_result)
from auction_game.reporting.run_paths import (OFFLINE_RUN_TYPES, RUN_TYPES, RunPaths, make_run_dir,
                                              run_type as run_type_of)

# 场次快照里实际调用的模型只记这些字段，来源和推理档位已在 capabilities 里。
EXECUTION_SNAPSHOT_FIELDS = ("key", "model", "provider", "protocol", "max_tokens", "model_settings", "capabilities")


def execution_calls(player_configs) -> dict[str, dict]:
    """玩家到实际调用的模型参数，就是写进场次快照的 execution_model。"""
    return {pc.profile.name: {field: getattr(pc.execution, field) for field in EXECUTION_SNAPSHOT_FIELDS}
            for pc in player_configs}


def call_label(call) -> str:
    """开赛打印阵容用：实际调用的型号和推理档位。"""
    return model_label(call.model, call.capabilities["reasoning_status"])


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


def root_rules(game: GameSettings, *, team: bool) -> tuple[Path, str]:
    """根目录规则的路径和内容：个人赛是 RULES.md，团队赛是 RULES_TEAM.md，角色名取自阵容配置，不读能力快照。"""
    lineup = Lineup(tuple(profile for profile, _ in (load_teams() if team else load_players())))
    return TEAM_RULES_PATH if team else RULES_PATH, rules_document(game, lineup, root=True)


def sync_root_rules(game: GameSettings, *, team: bool) -> None:
    """内容变了才重写根目录规则。"""
    path, text = root_rules(game, team=team)
    if not path.exists() or path.read_text(encoding="utf-8") != text:
        path.write_text(text, encoding="utf-8")


@dataclass(frozen=True)
class StartSetup:
    config: dict
    player_configs: list[PlayerConfig]
    game: GameSettings
    rules: str  # 按本场参数生成的规则


def prepare_start(*, test_model: bool, team: bool) -> StartSetup:
    """新开一场的准备，真实比赛和离线演示共用：读配置和本场参数，同步根规则，生成本场规则。

    配置或规则有误时在这里报错，这时还没创建场次目录。
    """
    config, player_configs = load_configuration(test_model=test_model, team=team)
    game = load_game_settings(test_model=test_model)
    # 根规则按生产参数生成；测试模式的覆盖参数只用于本场，要单独生成一次。
    sync_root_rules(load_game_settings(), team=team)
    rules = rules_document(game, Lineup(tuple(pc.profile for pc in player_configs)), root=False)
    return StartSetup(config, player_configs, game, rules)


def run_ai(test_model: bool = False, team: bool = False) -> int:
    """真实模型比赛，team 时是团队赛；配置、规则或密钥有误时在创建场次目录前停止。"""
    try:
        setup = prepare_start(test_model=test_model, team=team)
        config, player_configs = setup.config, setup.player_configs
        controller = Controller(mode="step", max_concurrency=config["llm"]["max_concurrency"])
        task_runners = make_task_runners(config["llm"], execution_calls(player_configs), make_model_factory,
                                         signals=controller.signals)
    except (ConfigurationError, MissingEnvError) as exc:
        print(f"[配置错误] {exc}")
        return 1
    run_type = run_type_of(team=team, test_model=test_model)
    run_dir = make_run_dir(run_type)
    if test_model:
        test_key = config["test_model"]
        print(f"测试模型：{test_key} / {call_label(player_configs[0].execution)}（全员使用，调用真实服务，可能收费）")
        print("角色身份与独立历史保持不变。")
    else:
        print("常规阵容（调用真实服务，可能收费）：")
        for pc in player_configs:
            print(f"  {pc.profile.name}：{call_label(pc.execution)}")
    warned = {pc.execution.key: pc.execution for pc in player_configs
              if pc.execution.capabilities["output_fallback_reason"]}
    for model in warned.values():
        label = "测试模型" if test_model else model.model
        print(f"[预算提醒] {label}：{model.capabilities['output_fallback_reason']}；暂用 {model.max_tokens} tokens。")
    if warned:
        print("请运行 python -m auction_game.cli.sync_models --write 更新模型能力信息；"
              "仍有提醒时检查 configs/models.yaml 的 models_dev 来源。")
    return play_ai(setup, task_runners, controller,
                   run_dir=run_dir, run_type=run_type, test_model=test_model, interactive=True)


def play_ai(setup: StartSetup, task_runners, controller, *, run_dir, run_type, test_model,
            interactive=False) -> int:
    """写场次快照并跑完整场 AI 比赛；离线演示传入假模型的任务入口和不交互的控制器。"""
    config, player_configs, game = setup.config, setup.player_configs, setup.game
    lineup = Lineup(tuple(pc.profile for pc in player_configs))
    calls = execution_calls(player_configs)
    # 本场配置快照，统计和展示只读它，续跑按它重建阵容和模型。
    snapshot = {
        "run_type": run_type,
        "mode": "test_model" if test_model else "regular",
        "game": game.to_dict(),
        "prompts": dict(config["prompts"]),
        "players": [{
            **asdict(pc.profile),
            "identity_model": pc.identity.to_dict(),
            "execution_model": calls[pc.profile.name],
        } for pc in player_configs],
    }
    paths = initialize_run(run_dir, snapshot, setup.rules)

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
    _print_llm_limits(controller.max_concurrency, config["llm"]["timeout_seconds"])
    prompt_options = config["prompts"]
    if lineup.teams:
        predictions = "不用于团队赛"
    else:
        predictions = "开启" if prompt_options["enable_predictions"] else "关闭"
    print(f"提示设置：赛前预测{predictions}，"
          f"人设{'开启' if prompt_options['enable_personas'] else '关闭'}，"
          f"写作要求{'开启' if prompt_options['enable_writing_style'] else '关闭'}")
    return _play(players, controller, run_dir, game, lineup, resuming=False, interactive=interactive)


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
    except (ConfigurationError, MissingEnvError) as exc:
        print(f"[配置错误] {exc}")
        return 1
    except ResumeError as exc:
        print(f"[无法续跑] {exc}")
        return 1
    print(f"续跑：{run_dir}（{RUN_TYPES[snapshot['run_type']]}，调用真实服务，可能收费）")
    _print_llm_limits(controller.max_concurrency, llm["timeout_seconds"])
    return continue_run(run_dir, task_runners, controller, interactive=True)


def continue_run(run_dir: Path, task_runners: dict, controller, *, interactive: bool = False) -> int:
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
    return _play(players, controller, run_dir, game, lineup, resuming=True, interactive=interactive)


def _print_llm_limits(max_concurrency: int, timeout_seconds: int | float) -> None:
    print(f"并发上限：{max_concurrency}，单次请求超时：{timeout_seconds} 秒")


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
          resuming: bool, interactive: bool) -> int:
    """interactive 是真实比赛：读终端命令，程序错误时打印报错和续跑命令；离线赛和测试把程序错误抛出去。"""
    if interactive:
        controller.start_terminal_input()
        print("操作：回车=下一轮 / c=连续 / s=单步 / p=暂停 / q 或 Ctrl+C=退出比赛")

    resume_command = f'python main.py --resume "{run_dir}"'
    try:
        result = AIDirector(players, controller, run_dir, game=game, team_of=lineup.team_of, resuming=resuming).run()
    except TournamentAborted:
        print(f"\n已退出比赛。续跑：{resume_command}")
        return 0
    except ResumeError as exc:
        print(f"\n[无法续跑] {exc}")
        return 1
    except Exception:
        if not interactive:
            raise
        traceback.print_exc()
        print(f"\n[程序错误] 已退出比赛，成功的结果续跑时沿用。续跑：{resume_command}")
        return 1
    print_final_result(result)
    return 0
