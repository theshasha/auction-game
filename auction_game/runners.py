"""加载比赛配置、组装 AI 玩家并启动个人赛或团队赛；续跑时按场次快照和任务日志接着跑。"""

from __future__ import annotations

import traceback
from collections import Counter
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
from auction_game.reporting.analyze import played_runs
from auction_game.reporting.estimate import SeatModel, collect_history, estimate, format_estimate
from auction_game.reporting.run_output import (find_unfinished_run, initialize_run, is_finished, model_label,
                                               print_final_result)
from auction_game.reporting.run_paths import (OFFLINE_RUN_TYPES, RUN_TYPES, RunPaths, make_run_dir,
                                              run_type as run_type_of)

# 场次快照里实际调用的模型只记这些字段，来源、推理档位和官方单价已在 capabilities 里。
# 价格倍率按开赛时记下，续跑换渠道或改倍率也不变。
EXECUTION_SNAPSHOT_FIELDS = ("key", "model", "provider", "protocol", "max_tokens", "model_settings", "capabilities",
                             "price_multiplier")


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
    """rules/ 下规则的路径和内容：个人赛是 RULES.md，团队赛是 RULES_TEAM.md，角色名取自阵容配置，不读能力快照。"""
    lineup = Lineup(tuple(profile for profile, _ in (load_teams() if team else load_players())))
    return TEAM_RULES_PATH if team else RULES_PATH, rules_document(game, lineup, root=True)


def sync_root_rules(game: GameSettings, *, team: bool) -> None:
    """内容变了才重写 rules/ 下的规则。"""
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
    for line in estimate_lines(player_configs, setup.game, team=team):
        print(line)
    return play_ai(setup, task_runners, controller, run_type=run_type, test_model=test_model, interactive=True)


def estimate_lines(player_configs: list[PlayerConfig], game: GameSettings, *, team: bool) -> list[str]:
    """按阵容和比赛参数预估花费的打印行；单价取当前能力快照，倍率取当前 models.yaml，用量取 logs/ 下的历史场次。"""
    seats = Counter((pc.execution.key, pc.execution.capabilities["reasoning_status"]) for pc in player_configs)
    executions = {(pc.execution.key, pc.execution.capabilities["reasoning_status"]): pc.execution
                  for pc in player_configs}
    models = [SeatModel(key, effort, call_label(executions[key, effort]), count,
                        executions[key, effort].capabilities.get("cost"), executions[key, effort].price_multiplier)
              for (key, effort), count in seats.items()]
    history = collect_history(played_runs())
    return format_estimate(estimate(models, game, team=team, history=history), game, team=team)


def play_ai(setup: StartSetup, task_runners, controller, *, run_type, test_model, run_dir: Path | None = None,
            interactive=False) -> int:
    """跑完整场 AI 比赛；离线演示传入假模型的任务入口和不交互的控制器。

    场次目录、快照、规则、README 和玩家日志都在第一轮放行后才写；run_dir 为 None 时这时在 logs/ 下新建目录。
    """
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
    # 系统提示先记在日志里，建好目录后写出。
    logs = [AgentLog(profile.name) for profile in lineup.profiles]
    players = [
        _make_player(
            profile, lineup, game, task_runners[profile.name], log,
            build_system_prompt(
                profile, lineup, game,
                enable_predictions=config["prompts"]["enable_predictions"],
                enable_personas=config["prompts"]["enable_personas"],
                writing_style=config["writing_style"],
            ))
        for profile, log in zip(lineup.profiles, logs)
    ]

    def write_new_run(target: Path) -> None:
        paths = initialize_run(target, snapshot, setup.rules)
        for number, log in enumerate(logs, 1):
            log.bind(paths.player(number), paths.player(number, readable=True))

    _print_llm_limits(controller.max_concurrency, config["llm"]["timeout_seconds"])
    prompt_options = config["prompts"]
    if lineup.teams:
        predictions = "不用于团队赛"
    else:
        predictions = "开启" if prompt_options["enable_predictions"] else "关闭"
    print(f"提示设置：赛前预测{predictions}，"
          f"人设{'开启' if prompt_options['enable_personas'] else '关闭'}，"
          f"写作要求{'开启' if prompt_options['enable_writing_style'] else '关闭'}")
    director = AIDirector(players, controller, None, game=game, team_of=lineup.team_of,
                          new_run_dir=lambda: make_run_dir(run_type) if run_dir is None else run_dir,
                          write_new_run=write_new_run)
    return _play(director, interactive=interactive)


def resume_ai(run_dir: Path | None = None) -> int:
    """接着跑一场没打完的真实模型比赛；run_dir 没给时取 logs/ 下最新一场没打完的。

    参数、阵容和每个席位调用的模型取自场次快照，渠道地址、超时、并发和密钥取自当前配置。
    rules/ 下的规则反映当前配置，和这一场无关，续跑不生成。
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
    return _play(AIDirector(players, controller, run_dir, game=game, team_of=lineup.team_of, resuming=True),
                 interactive=interactive)


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


def _play(director: AIDirector, *, interactive: bool) -> int:
    """interactive 是真实比赛：读终端命令，程序错误时打印报错和续跑命令；离线赛和测试把程序错误抛出去。

    新开的场次在第一轮放行前退出时还没有场次目录，场次文件没写全就出错时目录不完整，这两种都不给续跑命令。
    """
    if interactive:
        director.controller.start_terminal_input()
        print("操作：回车=下一轮 / c=连续 / s=单步 / p=暂停 / q 或 Ctrl+C=退出比赛")

    def resume_note() -> str:
        if director.paths is None:
            # 按 q 时控制器说过"已成功的结果续跑时沿用"，这里说明这次没有可续的。
            return "还没建场次，没有可续跑的内容。"
        if not director.files_ready:
            return f"场次目录没写完，删掉 {director.paths.root} 后重新开一场。"
        return f'续跑：python main.py --resume "{director.paths.root}"'

    try:
        result = director.run()
    except TournamentAborted:
        print(f"\n已退出比赛。{resume_note()}")
        return 0
    except ResumeError as exc:
        print(f"\n[无法续跑] {exc}")
        return 1
    except Exception:
        if not interactive:
            raise
        traceback.print_exc()
        kept = "成功的结果续跑时沿用。" if director.paths is not None and director.files_ready else ""
        print(f"\n[程序错误] 已退出比赛，{kept}{resume_note()}")
        return 1
    print_final_result(result)
    return 0
