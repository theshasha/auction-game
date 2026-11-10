"""analyze 测试的合成对局和场次目录。

合成对局（两局，每天一行出价 = NAMES 顺序）：

第 1 局
  第 1 天：出价 [5,4,3,2,1,0]；乙 狙 5（命中）、丙 狙 6（落空）；
            5 被撞车（甲 扣款 5），乙 以 4 获胜。
  第 2 天：出价 [3,3,1,2,0,6]；3 互撞（甲、乙 扣款 3），己 以 6 获胜。
  拍卖分：乙 1、己 1；乙命中另得 2 分，独占最高。
第 2 局
  第 1 天：出价 [0,3,0,2,5,4]；0 互撞，戊 以 5 获胜。
  第 2 天：出价 [0,4,1,2,3,2]；2 互撞（丁、己 扣款 2），乙 以 4 获胜。
  拍卖分：乙 1、戊 1（乙/戊 各记并列最高 1 局）。

局末余额按 game（初始 7、日工资 3、超级出价成本 2、悬赏 3）手算后写进日志。
"""

import json
from copy import deepcopy
from dataclasses import asdict, replace

from auction_game.agent.log import AgentLog
from auction_game.reporting.run_output import initialize_run
from auction_game.reporting.run_paths import RunPaths
from tests.support.factories import TEAM_OF, TEST_GAME, make_day, make_log, make_standings

ANALYZE_GAME = replace(TEST_GAME, daily_pool=(1, 1))


def make_match_1() -> dict:
    days = [
        make_day(1, [5, 4, 3, 2, 1, 0], super_bids={"乙": 5, "丙": 6}),
        make_day(2, [3, 3, 1, 2, 0, 6]),
    ]
    return make_log(match_index=1, days=days, game=ANALYZE_GAME).to_dict()


def make_match_2() -> dict:
    days = [
        make_day(1, [0, 3, 0, 2, 5, 4]),
        make_day(2, [0, 4, 1, 2, 3, 2]),
    ]
    return make_log(match_index=2, days=days, game=ANALYZE_GAME).to_dict()


def make_matches() -> list[dict]:
    return [
        {"match": make_match_1(), "standings_after_match": []},
        {"match": make_match_2(), "standings_after_match": []},
    ]


def write_matches(run_dir, matches, *, run_type="offline", players=None, team_of=None):
    """team_of 是团队赛的玩家到阵营，写进快照和每局的累计排名。"""
    first = matches[0]["match"]
    roster = deepcopy(players) if players is not None else [{"name": name} for name in first["auction_scores"]]
    for player in roster:
        player.setdefault("identity_model", {"model": "synthetic-identity"})
        player.setdefault("execution_model", {"protocol": "chat", "model": "synthetic-execution"})
        player["team"] = team_of[player["name"]] if team_of is not None else None
    paths = initialize_run(run_dir, {"run_type": run_type,
                                     "mode": "test_model" if run_type.endswith("-test") else "regular",
                                     "game": first["game"], "players": roster}, "test rules")
    # 模型玩家创建时就写下系统提示，每人都有任务日志。
    for pc in paths.read_config()["players"]:
        player_log(run_dir, pc).system_prompt("test system prompt")
    auction_scores_history = {name: [] for name in first["auction_scores"]}
    scores_history = {name: [] for name in first["scores"]}
    for payload in sorted(matches, key=lambda payload: payload["match"]["match_index"]):
        # 用生产排名函数准备磁盘元数据；各局分数取自场景的局日志。
        for name in auction_scores_history:
            auction_scores_history[name].append(payload["match"]["auction_scores"].get(name, 0))
            scores_history[name].append(payload["match"]["scores"].get(name, 0))
        payload["standings_after_match"] = [asdict(entry) for entry in make_standings(
            auction_scores_history, scores_history=scores_history, team_of=team_of)]
        paths.match(payload["match"]["match_index"]).write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )


def player_log(run_dir, pc):
    """用生产日志写入器追加到场次里这名玩家的任务日志。"""
    paths = RunPaths(run_dir)
    return AgentLog(pc["name"], paths.player(pc["number"]), readable_path=paths.player(pc["number"], readable=True))


def make_team_match() -> dict:
    """按阵营结算的一局，甲乙丙红队、丁戊己蓝队。

    第 1 天：出价 [3,3,5,5,5,1]；甲乙同出 3，红队队内撞车；丁戊同出 5，另有红队丙，蓝队也算队内撞车。
              丙狙 3 只狙到队友，丁狙 6 是空位，己狙 5 命中丙；己以 1 获胜。
    第 2 天：出价 [2,2,2,0,3,6]；红队三人同出 2，算一次队内撞车。甲狙 3 命中戊，戊狙 6 只狙到队友己；己以 6 获胜。
    """
    days = [
        make_day(1, [3, 3, 5, 5, 5, 1], super_bids={"丙": 3, "丁": 6, "己": 5}, game=ANALYZE_GAME, team_of=TEAM_OF),
        make_day(2, [2, 2, 2, 0, 3, 6], super_bids={"甲": 3, "戊": 6}, game=ANALYZE_GAME, team_of=TEAM_OF),
    ]
    return make_log(days=days, game=ANALYZE_GAME).to_dict()


# 团队赛对比用的参数：出价上限 7、超级出价成本 2 都不是生产值；日工资 1 小于成本，第 2 天能出现余额不够开枪的人。
TEAM_REPORT_GAME = replace(TEST_GAME, daily_salary=1, daily_pool=(1, 2))


def make_team_report_match(match_index=1) -> dict:
    """看余额狙击的一局，甲乙丙红队、丁戊己蓝队。

    第 1 天：全员余额 7，出价 [7,0,1,2,3,4]；戊狙 5（红队带狙的上限）落空；甲以 7 获胜，余额剩 0。
    第 2 天：余额 甲1 乙8 丙8 丁8 戊6 己8，出价 [1,6,7,2,0,4]。
              乙狙 2 命中丁，丁的余额价是 7、6，2 不是；
              丁狙 1 命中甲（余额 1 不够开枪，余额价只有 1），戊狙 6 命中乙（带狙出余额减 2），
              己狙 7 命中丙（余额 8，不带狙的上限封顶 7）；己以 4 获胜。
              去掉全部超级出价时丙以 7 获胜，超级出价把这天从红队换成蓝队。
    """
    days = [
        make_day(1, [7, 0, 1, 2, 3, 4], pool=1, super_bids={"戊": 5}, game=TEAM_REPORT_GAME, team_of=TEAM_OF),
        make_day(2, [1, 6, 7, 2, 0, 4], pool=2, super_bids={"乙": 2, "丁": 1, "戊": 6, "己": 7},
                 game=TEAM_REPORT_GAME, team_of=TEAM_OF),
    ]
    return make_log(match_index=match_index, days=days, game=TEAM_REPORT_GAME).to_dict()


def make_team_void_match() -> dict:
    """超级出价造成流局、两队都有队内撞车的一天。

    出价 [5,1,1,3,0,0]；乙丙同出 1、戊己同出 0；乙狙 3 命中丁，戊狙 5 命中甲，当天流局。
    去掉全部超级出价时甲以 5 获胜。只留乙丙中的一人，红队以 1 获胜；只留戊己中的一人，蓝队以 0 获胜。
    """
    day = make_day(1, [5, 1, 1, 3, 0, 0], pool=1, super_bids={"乙": 3, "戊": 5}, game=TEAM_REPORT_GAME,
                   team_of=TEAM_OF)
    return make_log(days=[day], game=TEAM_REPORT_GAME).to_dict()
