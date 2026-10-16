"""加载比赛配置、组装 Bot/AI 玩家并启动个人赛。"""

from __future__ import annotations

import random

from auction_game.ai.ai_player import AIPlayer
from auction_game.ai.ai_speech import AISpeechDirector
from auction_game.configuration import ConfigurationError, EXECUTION_FIELDS, channel_model_profile, load_configuration, load_game_settings
from auction_game.controller import (
    Controller,
    RoundTask,
    TournamentAborted,
    run_parallel_round,
)
from auction_game.ai.llm_client import MissingApiKeyError, make_model_factory
from auction_game.ai.agent_tasks import TaskRunner
from auction_game.game.players import BOT_NAMES, RandomBot
from auction_game.game.rules import render_rules, sync_root_rules
from auction_game.reporting.run_output import initialize_run, make_publish, make_run_dir, print_final_result
from auction_game.game.tournament import run_tournament


def run_bots(seed) -> int:
    try:
        game = load_game_settings()
        sync_root_rules(game)
    except ConfigurationError as exc:
        print(f"错误：配置读取失败：{exc}")
        return 1
    rng = random.Random(seed)
    players = [RandomBot(name, random.Random(rng.random())) for name in BOT_NAMES]
    run_dir = make_run_dir("bot")
    initialize_run(run_dir, {"run_type": "bot", "mode": "regular", "game": game.to_dict(),
                            "players": [{"name": p.name} for p in players]},
                   render_rules(game, [p.name for p in players]))
    result = run_tournament(players, game, on_match_end=make_publish(run_dir))
    print_final_result(result)
    return 0


def run_ai(*, model_factory=None, run_dir=None, controller=None, record_player=None,
           start_reader=True, test_model=False, run_type=None, loaded_configuration=None) -> int:
    """生产组装入口；离线演示仅注入模型边界、输出目录及控制器推进方式。

    record_player 按需记录单玩家完整逻辑请求，默认不膨胀普通比赛日志。
    loaded_configuration 供离线入口复用本次启动已解析的配置，不再次读取 YAML。
    """
    try:
        if loaded_configuration is None:
            loaded_configuration = load_configuration(test_model=True) if test_model else load_configuration()
        config, player_configs = loaded_configuration
        game = load_game_settings()
        sync_root_rules(game)
    except ConfigurationError as exc:
        print(f"错误：配置读取失败：{exc}")
        return 1
    if len(player_configs) < 2:
        print("错误：AI 对局至少需要 2 名玩家，请检查 players.yaml")
        return 1
    timeout = config["llm"]["timeout_seconds"]
    max_concurrency = config["llm"]["max_concurrency"]

    all_names = [pc["name"] for pc in player_configs]
    predictions = [(pc["name"], pc.get("predicted_win_rate", 0)) for pc in player_configs]

    task_runners = {}
    model_factory = model_factory or make_model_factory
    for pc in player_configs:
        try:
            task_runners[pc["name"]] = TaskRunner(model_factory(
                provider=pc["provider"],
                channel=config["llm"]["channels"][pc["provider"]],
                model=pc["model"],
                timeout=timeout,
                protocol=pc["protocol"],
                max_tokens=pc["max_tokens"],
                model_settings=pc["model_settings"],
            ), capture_requests=pc["name"] == record_player)
        except (MissingApiKeyError, ValueError) as exc:
            print(f"错误：玩家 {pc['name']} 的 LLM 配置不可用：{exc}")
            return 1

    run_type = run_type or ("ai-test" if test_model else "ai")
    run_dir = run_dir if run_dir is not None else make_run_dir(run_type)
    run_dir.mkdir(parents=True, exist_ok=True)
    # 只保存本场公开配置，不落环境变量值；回放无需读取今天的配置或密钥。
    snapshot = {
        "run_type": run_type,
        "mode": "test_model" if test_model else "regular",
        "game": game.to_dict(),
        "timeout_seconds": timeout,
        "max_concurrency": max_concurrency,
        "players": [{
            "name": pc["name"], "persona_title": pc["persona_title"], "persona_text": pc["persona_text"],
            "predicted_win_rate": pc.get("predicted_win_rate", 0),
            "identity_model": pc["identity_model"],
            "execution_model": {"key": pc["model_key"], **{key: pc[key] for key in
                                 EXECUTION_FIELDS},
                                "model_profile": channel_model_profile(
                                    config["llm"]["channels"][pc["provider"]], pc["protocol"]),
                                 "base_url": config["llm"]["channels"][pc["provider"]]["base_url"]},
        } for pc in player_configs],
    }
    paths = initialize_run(run_dir, snapshot, render_rules(game, all_names))
    controller = controller if controller is not None else Controller(mode="step")
    # 注入的离线控制器也使用同一份已校验配置，所有轮级任务共用此上限。
    controller.max_concurrency = max_concurrency

    players = [
        AIPlayer(
            name=pc["name"],
            persona_title=pc["persona_title"],
            persona_text=pc["persona_text"],
            predictions=predictions,
            all_names=all_names,
            task_runner=task_runners[pc["name"]],
            game=game,
            log_path=paths.player(number),
            readable_path=paths.player(number, readable=True),
            capture_path=paths.capture(number) if pc["name"] == record_player else None,
        )
        for number, pc in enumerate(player_configs, 1)
    ]
    print("测试运行：全员使用配置的测试模型，角色身份与独立历史保持不变。" if test_model else "常规阵容运行")
    print(f"整轮任务并发上限：{max_concurrency}（实际不超过待执行任务数）")
    for pc in player_configs:
        print(f"{pc['name']}: 身份 {pc['identity_model']['model']} → 实际 {pc['provider']} / "
              f"{pc['protocol']} / {pc['model']}（人设：{pc['persona_title']}）")
        capabilities = pc["capabilities"]
        print(f"  比赛输出预算 {pc['max_tokens']}；推理 {capabilities['reasoning_status']}："
              f"{capabilities['reasoning_detail']}")
        if capabilities["output_fallback_reason"]:
            print(f"  输出回退：{capabilities['output_fallback_reason']}")

    if start_reader:
        controller.start_stdin_reader()
    print("命令：回车=推进一轮 / c=连续模式 / s=单步模式 / p=暂停（等待在途并保留成功，恢复仅补跑失败/未启动）/ q=中止比赛")

    def bid_collector(observations):
        sample_obs = observations[0][1]  # 同一轮内所有玩家的局/天相同
        label = f"第{len(sample_obs.past_match_logs) + 1}局第{sample_obs.day}天出价"
        tasks = [
            RoundTask(
                key=player.name,
                participant=player,
                fn=lambda player=player, obs=obs: player.decide_bid(obs),
            )
            for player, obs in observations
        ]
        results = run_parallel_round(tasks, controller, label=label)
        return [results[player.name] for player, _ in observations]

    director = AISpeechDirector(players, controller, run_dir, game=game)
    try:
        result = run_tournament(
            players,
            game,
            on_match_end=make_publish(run_dir),
            director=director,
            bid_collector=bid_collector,
        )
    except TournamentAborted:
        print("\n比赛已被用户中止。")
        return 0
    print_final_result(result)
    return 0
