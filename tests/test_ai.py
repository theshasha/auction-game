"""AI 玩家与控制器的测试：全部注入 fake chat_fn / 直接发命令，零网络。"""

import json
import threading

import pytest

from ai_player import AIPlayer, PARSE_FAILED
from controller import Controller, RoundAborted, RoundTask, TournamentAborted, run_parallel_round
from llm_client import ChatReply
from models import Observation

ALL_NAMES = ["Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot"]


def make_obs(day=1, coins=6):
    return Observation(
        my_name="Alpha",
        my_coins=coins,
        my_match_wins=0,
        my_total_wins=0,
        day=day,
        wins_remaining=3,
        max_days=5,
        today_history=[],
        standings=[],
        past_match_logs=[],
    )


def fake_chat_fn(script):
    """script: ChatReply 或异常的有序列表，每次调用弹出下一个。"""
    queue = list(script)
    calls = []

    def chat_fn(messages):
        calls.append(messages)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    chat_fn.calls = calls
    return chat_fn


def make_player(chat_fn, tmp_path=None):
    log_path = (tmp_path / "ai_Alpha.jsonl") if tmp_path else None
    return AIPlayer("Alpha", "测试人设", ALL_NAMES, chat_fn, log_path=log_path)


def bid_reply(bid, reasoning="思考内容"):
    return ChatReply(content=f"我的分析：……\n```json\n{{\"bid\": {bid}}}\n```", reasoning_content=reasoning)


class TestBidParsing:
    def test_normal_reply_parsed(self):
        player = make_player(fake_chat_fn([bid_reply(3)]))
        assert player.decide_bid(make_obs()) == 3

    def test_uses_last_json_block(self):
        content = "```json\n{\"bid\": 1}\n```\n改主意了\n```json\n{\"bid\": 4}\n```"
        player = make_player(fake_chat_fn([ChatReply(content=content, reasoning_content="")]))
        assert player.decide_bid(make_obs()) == 4

    def test_missing_json_block_retries_once(self):
        script = [
            ChatReply(content="我忘了给 JSON", reasoning_content=""),
            bid_reply(2),
        ]
        chat_fn = fake_chat_fn(script)
        player = make_player(chat_fn)
        assert player.decide_bid(make_obs()) == 2
        assert len(chat_fn.calls) == 2
        retry_user_msg = player.messages[-2]["content"]
        assert "缺少合法的 JSON" in retry_user_msg

    def test_double_parse_failure_returns_zero(self):
        script = [ChatReply(content="没有块", reasoning_content=""), ChatReply(content="还是没有", reasoning_content="")]
        player = make_player(fake_chat_fn(script))
        assert player.decide_bid(make_obs()) == 0

    def test_invalid_value_not_retried(self):
        # 数值非法（超上限）由引擎判 illegal，AIPlayer 原样返回且不重试
        chat_fn = fake_chat_fn([bid_reply(9)])
        player = make_player(chat_fn)
        assert player.decide_bid(make_obs()) == 9
        assert len(chat_fn.calls) == 1

    def test_api_error_returns_zero(self):
        player = make_player(fake_chat_fn([RuntimeError("boom")]))
        assert player.decide_bid(make_obs()) == 0


class TestConversationLine:
    def test_history_is_append_only(self):
        script = [bid_reply(1), bid_reply(2), bid_reply(3)]
        player = make_player(fake_chat_fn(script))
        prefix_snapshot = list(player.messages)
        player.decide_bid(make_obs(day=1))
        mid_snapshot = list(player.messages)
        player.decide_bid(make_obs(day=2))
        # 任意时刻已有消息逐字节不变
        assert player.messages[: len(prefix_snapshot)] == prefix_snapshot
        assert player.messages[: len(mid_snapshot)] == mid_snapshot
        assert player.messages[0]["role"] == "system"

    def test_reasoning_logged_but_not_in_context(self, tmp_path):
        player = make_player(fake_chat_fn([bid_reply(3, reasoning="秘密思考")]), tmp_path)
        player.decide_bid(make_obs())

        for message in player.messages:
            assert "秘密思考" not in json.dumps(message, ensure_ascii=False)

        log_entries = [json.loads(line) for line in player.log_path.read_text(encoding="utf-8").splitlines()]
        assert any(entry.get("reasoning_content") == "秘密思考" for entry in log_entries)

    def test_rollback_truncates_only_tail(self):
        player = make_player(fake_chat_fn([bid_reply(1), bid_reply(2)]))
        player.decide_bid(make_obs(day=1))
        checkpoint = player.checkpoint()
        prefix = list(player.messages)
        player.decide_bid(make_obs(day=2))
        player.rollback(checkpoint)
        assert player.messages == prefix


class TestSpeech:
    def test_speech_truncated_at_500_chars(self, tmp_path):
        long_speech = "很" * 600
        player = make_player(fake_chat_fn([ChatReply(content=long_speech, reasoning_content="")]), tmp_path)
        speech = player.give_speech("请发言", "opening_speech")
        assert speech is not None and len(speech) == 500
        log_entries = [json.loads(line) for line in player.log_path.read_text(encoding="utf-8").splitlines()]
        assert any(entry.get("truncated") for entry in log_entries)

    def test_speech_error_means_absent(self):
        player = make_player(fake_chat_fn([RuntimeError("timeout")]))
        assert player.give_speech("请发言", "opening_speech") is None

    def test_final_summary_not_added_to_context(self):
        player = make_player(fake_chat_fn([ChatReply(content="给观众的话", reasoning_content="")]))
        before = list(player.messages)
        summary = player.final_summary("请写观众总结")
        assert summary == "给观众的话"
        assert player.messages == before  # 连 user 消息也不进对话线


class TestController:
    def test_step_mode_gates_each_call(self):
        controller = Controller(mode="step", announce=lambda msg: None)
        done = []

        def call_once():
            controller.wait_for_turn("测试调用")
            done.append(1)

        thread = threading.Thread(target=call_once)
        thread.start()
        thread.join(timeout=0.2)
        assert not done  # 未确认前阻塞
        controller.offer_command("")  # 回车放行
        thread.join(timeout=1)
        assert done == [1]

    def test_continuous_mode_passes_through(self):
        controller = Controller(mode="continuous", announce=lambda msg: None)
        controller.wait_for_turn("任意调用")  # 不阻塞，直接返回

    def test_pause_raises_round_aborted(self):
        controller = Controller(mode="step", announce=lambda msg: None)
        controller.offer_command("p")
        with pytest.raises(RoundAborted):
            controller.wait_for_turn("测试调用")

    def test_quit_raises_tournament_aborted(self):
        controller = Controller(mode="continuous", announce=lambda msg: None)
        controller.offer_command("q")
        with pytest.raises(TournamentAborted):
            controller.wait_for_turn("测试调用")

    def test_mode_switch_anytime(self):
        controller = Controller(mode="step", announce=lambda msg: None)
        controller.offer_command("c")
        assert controller._mode == "continuous"
        controller.offer_command("s")
        assert controller._mode == "step"


class TestParallelRoundRollback:
    def test_pause_discards_round_and_rolls_back(self):
        controller = Controller(mode="step", announce=lambda msg: None)
        player = make_player(fake_chat_fn([bid_reply(1)]))

        def bidding_task():
            return player.decide_bid(make_obs())

        tasks = [
            RoundTask(key="Alpha", description="Alpha 出价", player=player, fn=bidding_task),
            RoundTask(key="Bravo", description="Bravo 出价", player=make_player(fake_chat_fn([])), fn=lambda: 0),
        ]
        before = {task.key: task.player.messages[:] for task in tasks}

        def run():
            with pytest.raises(RoundAborted):
                run_parallel_round(tasks, controller)

        thread = threading.Thread(target=run)
        thread.start()
        controller.offer_command("")   # 放行 Alpha（完成一次真实调用，消息已追加）
        controller.offer_command("p")  # Bravo 执行前暂停
        thread.join(timeout=2)

        assert thread.is_alive() is False
        for task in tasks:  # Alpha 已完成的对话也被回滚
            assert task.player.messages == before[task.key]

    def test_completed_round_returns_all_results(self):
        controller = Controller(mode="continuous", announce=lambda msg: None)
        tasks = [
            RoundTask(key=str(i), description=f"任务{i}", player=None, fn=lambda i=i: i * 10)
            for i in range(6)
        ]
        results = run_parallel_round(tasks, controller)
        assert results == {str(i): i * 10 for i in range(6)}
