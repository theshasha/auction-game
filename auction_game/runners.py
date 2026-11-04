"""加载比赛配置、组装 AI 玩家并启动个人赛。"""

from __future__ import annotations

from dataclasses import asdict

from auction_game.flow.director import AIDirector
from auction_game.agent.player import AIPlayer
from auction_game.configuration import ConfigurationError, load_configuration, load_game_settings
from auction_game.flow.controller import Controller, TournamentAborted
from auction_game.llm.client import MissingApiKeyError, make_model_factory
from auction_game.agent.tasks import TaskRunner
from auction_game.cli.rules import sync_root_rules
from auction_game.game.lineup import Lineup
from auction_game.game.rules import RulesError, render_rules
from auction_game.reporting.run_output import initialize_run, make_run_dir, print_final_result

# 场次快照里实际调用的模型只记这些字段，来源和推理档位已在 capabilities 里。
EXECUTION_SNAPSHOT_FIELDS = ("key", "model", "provider", "protocol", "max_tokens", "model_settings", "capabilities")


def make_task_runners(config, player_configs, model_factory, runner=TaskRunner, signals=None) -> dict:
    """为每名玩家创建任务入口；缺密钥在这里报错，早于创建场次目录。"""
    llm = config["llm"]
    return {pc.profile.name: runner(model_factory(
        provider=pc.execution.provider,
        channel=llm["channels"][pc.execution.provider],
        model=pc.execution.model,
        timeout=llm["timeout_seconds"],
        protocol=pc.execution.protocol,
        max_tokens=pc.execution.max_tokens,
        model_settings=pc.execution.model_settings,
        signals=signals,
    )) for pc in player_configs}


def run_ai(test_model: bool = False) -> int:
    """真实模型比赛；配置、规则或密钥有误时在创建场次目录前停止。"""
    try:
        config, player_configs = load_configuration(test_model=test_model)
        game = load_game_settings(test_model=test_model)
        sync_root_rules(load_game_settings())
        # 根规则按生产参数生成；测试模式的覆盖参数只用于本场，要单独生成一次。
        rules = render_rules(game, [pc.profile.name for pc in player_configs])
        controller = Controller(mode="step", max_concurrency=config["llm"]["max_concurrency"])
        task_runners = make_task_runners(config, player_configs, make_model_factory,
                                         signals=controller.signals)
    except (ConfigurationError, RulesError, MissingApiKeyError) as exc:
        print(f"[配置错误] {exc}")
        return 1
    run_type = "ai-test" if test_model else "ai"
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
    predictions = [(profile.name, profile.predicted_win_rate) for profile in lineup.profiles]
    # 本场配置快照，统计和展示只读它。
    snapshot = {
        "run_type": run_type,
        "mode": "test_model" if test_model else "regular",
        "game": game.to_dict(),
        "max_concurrency": controller.max_concurrency,
        "prompts": dict(config["prompts"]),
        "players": [{
            **asdict(pc.profile),
            "identity_model": pc.identity.to_dict(),
            "execution_model": {field: getattr(pc.execution, field) for field in EXECUTION_SNAPSHOT_FIELDS},
        } for pc in player_configs],
    }
    paths = initialize_run(run_dir, snapshot, rules)

    players = [
        AIPlayer(
            name=profile.name,
            persona_title=profile.persona_title,
            persona_text=profile.persona_text,
            predictions=predictions,
            enable_predictions=config["prompts"]["enable_predictions"],
            enable_personas=config["prompts"]["enable_personas"],
            writing_style=config["writing_style"],
            all_names=lineup.names,
            task_runner=task_runners[profile.name],
            game=game,
            log_path=paths.player(number),
            readable_path=paths.player(number, readable=True),
        )
        for number, profile in enumerate(lineup.profiles, 1)
    ]
    print(f"并发上限：{controller.max_concurrency}")
    prompt_options = config["prompts"]
    print(f"提示设置：赛前预测{'开启' if prompt_options['enable_predictions'] else '关闭'}，"
          f"人设{'开启' if prompt_options['enable_personas'] else '关闭'}，"
          f"写作要求{'开启' if prompt_options['enable_writing_style'] else '关闭'}")
    if start_reader:
        controller.start_stdin_reader()
        print("操作：回车=下一轮 / c=连续 / s=单步 / p=暂停 / q=退出比赛")

    try:
        result = AIDirector(players, controller, run_dir, game=game).run()
    except TournamentAborted:
        print("\n已退出比赛。")
        return 0
    print_final_result(result)
    return 0
