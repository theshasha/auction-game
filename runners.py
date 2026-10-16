"""加载比赛配置、组装 Bot/AI 玩家并启动个人赛。"""

from __future__ import annotations

import random

from ai_player import AIPlayer
from ai_speech import AISpeechDirector
from configuration import ConfigurationError, load_configuration, load_game_settings
from controller import (
    Controller,
    RoundTask,
    TournamentAborted,
    run_parallel_round,
)
from llm_client import MissingApiKeyError, make_model_factory
from agent_tasks import TaskRunner
from players import BOT_NAMES, RandomBot
from rules import render_rules
from run_output import make_publish, make_run_dir, print_final_result
from tournament import run_tournament


def run_bots(seed) -> int:
    try:
        game = load_game_settings()
    except ConfigurationError as exc:
        print(f"错误：配置读取失败：{exc}")
        return 1
    rng = random.Random(seed)
    players = [RandomBot(name, random.Random(rng.random())) for name in BOT_NAMES]
    run_dir = make_run_dir("bot")
    (run_dir / "RULES.md").write_text(render_rules(game, [p.name for p in players]), encoding="utf-8")
    result = run_tournament(players, game, on_match_end=make_publish(run_dir))
    print_final_result(result)
    return 0


def run_ai(*, model_factory=None, run_dir=None, controller=None, record_player=None,
           start_reader=True) -> int:
    """生产组装入口；离线演示仅注入模型边界、输出目录及控制器推进方式。

    record_player 按需记录单玩家完整逻辑请求，默认不膨胀普通比赛日志。
    """
    try:
        config, player_configs = load_configuration()
        game = load_game_settings()
    except ConfigurationError as exc:
        print(f"错误：配置读取失败：{exc}")
        return 1
    if len(player_configs) < 2:
        print("错误：AI 对局至少需要 2 名玩家，请检查 players.yaml")
        return 1
    timeout = config["llm"]["timeout_seconds"]

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
            ), capture_requests=pc["name"] == record_player)
        except (MissingApiKeyError, ValueError) as exc:
            print(f"错误：玩家 {pc['name']} 的 LLM 配置不可用：{exc}")
            return 1

    run_dir = run_dir if run_dir is not None else make_run_dir("ai")
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "RULES.md").write_text(render_rules(game, all_names), encoding="utf-8")
    controller = controller if controller is not None else Controller(mode="step")

    players = [
        AIPlayer(
            name=pc["name"],
            persona_title=pc["persona_title"],
            persona_text=pc["persona_text"],
            predictions=predictions,
            all_names=all_names,
            task_runner=task_runners[pc["name"]],
            game=game,
            log_path=run_dir / f"ai_{pc['name']}.jsonl",
        )
        for pc in player_configs
    ]
    for pc in player_configs:
        print(f"{pc['name']}: {pc['provider']} / {pc['model']}（人设：{pc['persona_title']}）")

    if start_reader:
        controller.start_stdin_reader()
    print("命令：回车=推进一轮 / c=连续模式 / s=单步模式 / p=暂停（保留已完成，恢复后仅补跑未完成）/ q=中止比赛")

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
