"""回放按事件选节点，结算使用实际局日，失败场次也能离线阅读。"""

import pytest

from auction_game.ai.agent_execution import OutputFailedError
from auction_game.reporting.replay import _entry_day, _select_entries, _settlement_lines, export_replay
from auction_game.reporting.run_output import initialize_run
from tests.support.factories import (
    NAMES, TEST_GAME, bid_reply, fake_task_runner, make_ai_obs, make_day, make_log, make_player,
)


def test_events_are_first_only_and_share_the_actual_request():
    days = [
        make_day(1, [0] * 6, 4, None, None, [0], is_void=True, super_bids={NAMES[0]: 1}),
        make_day(2, [0, 1, 0, 0, 0, 2], 4, NAMES[1], 1, [0, 2], 4,
                 super_bids={NAMES[0]: (2, True), NAMES[1]: (2, True)}),
    ]
    matches = [{"match": make_log(days=days).to_dict()},
               {"match": make_log(match_index=2, is_playoff=True, days=days).to_dict()}]
    entries = [
        {"task": "plan", "phase": "opening_plan", "context": "开局", "status": "success"},
        {"task": "bid", "phase": "bid", "context": "第1局第2天", "status": "success"},
        {"task": "plan", "phase": "match_1_plan", "context": "第一局结束", "status": "success"},
        {"task": "bid", "phase": "bid", "context": "第2局第2天", "status": "success"},
        {"task": "bid", "phase": "bid", "context": "第2局第3天", "status": "success"},
    ]
    selected = _select_entries(entries, matches)
    assert set(selected) == {0, 1, 2, 3}
    assert "首次超级落空" in selected[0] and "首次流局" in selected[0]
    assert "首次超级命中" in selected[1]
    assert all("滚存" not in title for title in selected.values())
    assert selected[3] == "第2局第2天 · 出价 · 加赛关键日"
    assert _entry_day(entries[2]) == (2, 1)
    assert _entry_day(entries[3]) == (2, 2)
    assert all("首次" not in selected[i] for i in (2, 3))


def test_void_day_still_shows_each_super_hit_score():
    day = make_day(1, [0, 1, 0, 0, 0, 0], 4, None, None, [0, 1], is_void=True,
                   super_bids={NAMES[0]: (1, True), NAMES[1]: (0, True), NAMES[2]: (2, False)})
    match = make_log(days=[day]).to_dict()
    text = "\n".join(_settlement_lines(match, match["days"][0], NAMES[0]))
    assert "当日流局，奖池作废，拍卖得分 0 分" in text
    assert f"{NAMES[2]} 出 2，落空，命中得分 0 分" in text
    assert f"{NAMES[1]} 出 0，命中，命中得分 {TEST_GAME.super_hit_score} 分" in text
    assert f"{NAMES[1]} {TEST_GAME.super_hit_score} 分" in text


def test_failed_audit_exports_without_any_settled_match(tmp_path):
    paths = initialize_run(tmp_path, {
        "run_type": "offline", "mode": "regular", "game": TEST_GAME.to_dict(),
        "timeout_seconds": 3,
        "players": [{"name": "Alpha", "identity_model": {"model": "identity"},
                     "execution_model": {"model": "configured", "protocol": "chat", "max_tokens": 100,
                                         "base_url": "https://offline.invalid/v1"}}],
    }, "测试规则")
    runner = fake_task_runner([bid_reply(-1), bid_reply(-1)])
    runner.executor.capture_requests = True
    player = make_player(runner, tmp_path)
    with pytest.raises(OutputFailedError):
        player.decide_bid(make_ai_obs(day=2))
    player.log_path.rename(paths.player(1))
    paths.capture(1).parent.mkdir(parents=True)
    (tmp_path / "capture.jsonl").rename(paths.capture(1))
    replay = export_replay(tmp_path, "Alpha", capture_definitions=True).read_text(encoding="utf-8")
    assert "尚无已结算局日志" in replay and "任务失败" in replay
    assert "实际执行：假模型 FunctionModel；配置模型：chat / configured" in replay
    assert "身份模型：identity" in replay and "纠正后回复" in replay
