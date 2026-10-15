"""AI 玩家的测试：出价解析、对话线只增不改、md 双写日志。全部注入 fake chat_fn，零网络。
发言与规划合并见 test_ai_speech.py；控制器/并发执行器见 test_controller.py；
共享夹具见 conftest.py。"""

import json
from copy import deepcopy
from dataclasses import replace

import pytest

from ai_player import AIPlayer, CallFailedError, InvalidBidError, ParseFailedError
from conftest import ALL_NAMES, TEST_GAME, TEST_PREDICTIONS, bid_reply, fake_chat_fn, make_ai_obs, make_player, speech_reply
import prompts


class TestCustomGame:
    @staticmethod
    def make_player(chat_fn):
        game = replace(
            TEST_GAME, initial_coins=4, daily_salary=5, daily_pool=(2, 1, 3),
            max_bid=9, super_cost=3, super_bounty=6, regular_matches=2, max_playoffs=1,
        )
        return AIPlayer(
            "Alpha", "测试人设", "正文", TEST_PREDICTIONS, ALL_NAMES, chat_fn, game=game
        )

    def test_plan_uses_initial_balance_and_custom_cost(self):
        chat_fn = fake_chat_fn([bid_reply(5), '{"bid": 1, "super_bid": 9}'])
        player = self.make_player(chat_fn)
        player.plan_strategy(prompts.opening_plan_request({}, player.game), "opening_plan")
        assert "超过当前余额 4" in player.messages[-2]["content"]
        assert "0到9的整数" in player.messages[-2]["content"]
        assert "加 3 金币成本" in player.messages[-2]["content"]
        assert "你的金币 4，本局已获胜场 0，今日奖池 2 个胜场，出价上限 9" in player.messages[1]["content"]
        obs = replace(make_ai_obs(day=1, coins=4), game=player.game, today_pool=2)
        assert player.decide_bid(obs) == (1, 9)
        assert len(chat_fn.calls) == 2

    @pytest.mark.parametrize("first_reply", ["缺少 JSON", '{"bid": 8, "super_bid": 9}'])
    def test_daily_retry_formats_use_same_game(self, first_reply):
        chat_fn = fake_chat_fn([first_reply, '{"bid": 7, "super_bid": 9}'])
        player = self.make_player(chat_fn)
        obs = replace(make_ai_obs(day=2, coins=10), game=player.game)
        assert player.decide_bid(obs) == (7, 9)
        for user_text in (player.messages[1]["content"], player.messages[-2]["content"]):
            assert "0到9的整数" in user_text
            assert "正常出价加 3 金币成本" in user_text
        if first_reply.startswith("{"):
            assert "合计超过当前余额 10" in player.messages[-2]["content"]
        assert len(chat_fn.calls) == 2

    def test_failed_replan_clears_previous_day1_bid(self):
        chat_fn = fake_chat_fn([bid_reply(2), bid_reply(5), bid_reply(6)])
        player = self.make_player(chat_fn)
        player.plan_strategy("首局规划", "opening_plan")
        with pytest.raises(InvalidBidError):
            player.plan_strategy("下一局重新规划", "next_plan")
        assert player._pending_day1_bid is None
        assert len(chat_fn.calls) == 3


class TestBidParsing:
    @pytest.mark.parametrize("reply, expected", [
        ('分析：……\n{"bid": 4, "super_bid": 6}', (4, 6)),
        ('分析：……\n{"bid": 4, "super_bid": null}', (4, None)),
        ('```json\n{"bid": 1}\n```\n改主意了\n```json\n{"bid": 4}\n```', (4, None)),
        ('先举个例子 {"bid": 1}，但改主意了\n{"bid": 5}', (5, None)),
    ], ids=["super", "null-super", "last-fence", "last-bare-object"])
    def test_submission_formats(self, reply, expected):
        chat_fn = fake_chat_fn([reply])
        player = make_player(chat_fn)
        assert player.decide_bid(make_ai_obs()) == expected
        assert len(chat_fn.calls) == 1

    def test_bare_json_last_object_without_bid_key_fails(self):
        # 最后一个合法 JSON 无 bid 键：不再向前找，按解析失败处理（重试 1 次后成功）
        script = ['先出价 {"bid": 1}，最后改为 {"note": "x"}', bid_reply(3)]
        chat_fn = fake_chat_fn(script)
        player = make_player(chat_fn)
        assert player.decide_bid(make_ai_obs()) == (3, None)
        assert len(chat_fn.calls) == 2
        assert "缺少合法的 JSON" in player.messages[-2]["content"]

    def test_missing_json_block_retries_once(self):
        script = [
            "我忘了给 JSON",
            bid_reply(2),
        ]
        chat_fn = fake_chat_fn(script)
        player = make_player(chat_fn)
        assert player.decide_bid(make_ai_obs()) == (2, None)
        assert len(chat_fn.calls) == 2
        retry_user_msg = player.messages[-2]["content"]
        assert "缺少合法的 JSON" in retry_user_msg

    def test_double_parse_failure_escalates(self, tmp_path):
        # 原地重试仍解析失败：抛 ParseFailedError 升级轮级失败（挂起等人工重跑），
        # 不再静默记 0；ParseFailedError 是 CallFailedError 子类，轮级机制直接接住
        script = ["没有块", "还是没有"]
        chat_fn = fake_chat_fn(script)
        player = make_player(chat_fn, tmp_path)
        with pytest.raises(ParseFailedError) as exc_info:
            player.decide_bid(make_ai_obs())
        assert isinstance(exc_info.value, CallFailedError)
        assert "连续两次解析失败" in str(exc_info.value)
        assert len(chat_fn.calls) == 2
        # 垃圾问答对在对话线中（由轮级 rollback 清掉），日志含 parse_failed 标记
        log_entries = [
            json.loads(line) for line in player.log_path.read_text(encoding="utf-8").splitlines()
        ]
        assert any(entry.get("parse_failed") and "result" not in entry for entry in log_entries)
        assert [m["role"] for m in player.messages] == [
            "system", "user", "assistant", "user", "assistant",
        ]
        assert "缺少合法的 JSON" in player.messages[-2]["content"]
        md_text = player.log_path.with_suffix(".md").read_text(encoding="utf-8")
        assert "> bid：解析连续失败，已挂起等待人工重跑" in md_text

    def test_empty_reply_treated_as_call_failure(self):
        # 空回复（渠道故障症状）按调用失败处理：立即重试，第二次正常出价
        chat_fn = fake_chat_fn(["", bid_reply(4)])
        player = make_player(chat_fn)
        assert player.decide_bid(make_ai_obs()) == (4, None)
        assert len(chat_fn.calls) == 2
        assert [m["role"] for m in player.messages] == ["system", "user", "assistant"]

    def test_double_empty_reply_raises_call_failed(self, tmp_path):
        # 连续两次空回复：抛 CallFailedError 进轮级失败名单
        chat_fn = fake_chat_fn(["", "  \n"])
        player = make_player(chat_fn, tmp_path)
        with pytest.raises(CallFailedError) as exc_info:
            player.decide_bid(make_ai_obs())
        assert "EmptyResponseError" in str(exc_info.value)
        assert len(chat_fn.calls) == 2
        assert len(player.messages) == 1
        log_entries = [
            json.loads(line) for line in player.log_path.read_text(encoding="utf-8").splitlines()
        ]
        assert sum(1 for entry in log_entries if "EmptyResponseError" in entry.get("error", "")) == 2

    @pytest.mark.parametrize("reply, coins, reason", [
        (bid_reply(9), 6, "超出 0~7 的范围"),
        (bid_reply(6), 3, "超过当前余额 3"),
        ('{"bid": 3, "super_bid": 6}', 4, "合计超过当前余额 4"),
        ('{"bid": 3, "super_bid": 3}', 6, "不能等于你的正常出价"),
        ('{"bid": 3, "super_bid": 9}', 6, "超级出价 9 超出 0~7 的范围"),
    ], ids=["range", "balance", "total-cost", "same-super", "super-range"])
    def test_invalid_submission_retried_with_reason(self, reply, coins, reason):
        chat_fn = fake_chat_fn([reply, bid_reply(3)])
        player = make_player(chat_fn)
        assert player.decide_bid(make_ai_obs(coins=coins)) == (3, None)
        assert len(chat_fn.calls) == 2
        assert "上一提交非法" in player.messages[-2]["content"]
        assert reason in player.messages[-2]["content"]
        assert [m["role"] for m in player.messages] == [
            "system", "user", "assistant", "user", "assistant",
        ]
        assert chat_fn.calls[0] == player.messages[:2]
        assert chat_fn.calls[1] == player.messages[:4]

    def test_double_invalid_escalates_as_invalid_bid_error(self, tmp_path):
        # 提醒重试后仍非法：抛 InvalidBidError 升级轮级失败（游戏暂停等人工），
        # 不再由引擎静默记 0；InvalidBidError 是 CallFailedError 子类
        chat_fn = fake_chat_fn([bid_reply(9), bid_reply(8)])
        player = make_player(chat_fn, tmp_path)
        with pytest.raises(InvalidBidError) as exc_info:
            player.decide_bid(make_ai_obs())
        assert isinstance(exc_info.value, CallFailedError)
        assert "连续两次非法" in str(exc_info.value)
        assert len(chat_fn.calls) == 2

    def test_error_then_success_recovers_and_logs(self, tmp_path):
        chat_fn = fake_chat_fn([RuntimeError("timeout"), bid_reply(4)])
        player = make_player(chat_fn, tmp_path)
        assert player.decide_bid(make_ai_obs(day=2)) == (4, None)
        assert len(chat_fn.calls) == 2
        # 失败的 user 消息已回滚，对话线只有 system + 成功的一问一答
        assert [m["role"] for m in player.messages] == ["system", "user", "assistant"]
        assert chat_fn.calls[0] == chat_fn.calls[1] == player.messages[:2]
        entries = [json.loads(line) for line in player.log_path.read_text(encoding="utf-8").splitlines()]
        assert entries[0] == {
            "phase": "bid", "attempt": 1, "error": "RuntimeError: timeout", "context": "第1局第2天",
        }
        assert entries[1]["content"] == bid_reply(4)
        md_text = player.log_path.with_suffix(".md").read_text(encoding="utf-8")
        assert "> 调用失败（bid，第1局第2天，第 1 次）：RuntimeError: timeout" in md_text
        assert "## bid" in md_text

    def test_double_error_raises_call_failed(self):
        # 连续两次失败（基础设施故障）：抛 CallFailedError 交给轮级别重跑
        chat_fn = fake_chat_fn([RuntimeError("余额不足"), RuntimeError("余额不足")])
        player = make_player(chat_fn)
        with pytest.raises(CallFailedError) as exc_info:
            player.decide_bid(make_ai_obs())
        assert "连续两次调用失败" in str(exc_info.value)
        assert "余额不足" in str(exc_info.value)
        assert len(chat_fn.calls) == 2
        assert len(player.messages) == 1  # 两次失败的 user 消息均已回滚


class TestConversationLine:
    def test_history_is_append_only(self):
        chat_fn = fake_chat_fn([bid_reply(1), bid_reply(2)])
        player = make_player(chat_fn)
        prefix_snapshot = deepcopy(player.messages)
        player.decide_bid(make_ai_obs(day=1))
        mid_snapshot = deepcopy(player.messages)
        player.decide_bid(make_ai_obs(day=2))
        # 任意时刻已有消息逐字节不变
        assert player.messages[: len(prefix_snapshot)] == prefix_snapshot
        assert player.messages[: len(mid_snapshot)] == mid_snapshot
        assert player.messages[0]["role"] == "system"
        assert chat_fn.calls[0] == mid_snapshot[:-1]
        assert chat_fn.calls[1] == player.messages[:-1]

    def test_rollback_truncates_only_tail(self):
        player = make_player(fake_chat_fn([bid_reply(1), bid_reply(2)]))
        player.decide_bid(make_ai_obs(day=1))
        checkpoint = player.checkpoint()
        prefix = deepcopy(player.messages)
        player.decide_bid(make_ai_obs(day=2))
        player.rollback(checkpoint)
        assert player.messages == prefix


class TestMarkdownLog:
    """同次请求检查 JSONL 和 Markdown；保留历史渲染私有分支。"""

    @staticmethod
    def read_md(player) -> str:
        return player.log_path.with_suffix(".md").read_text(encoding="utf-8")

    def test_normal_bid_entry_rendered(self, tmp_path):
        player = make_player(fake_chat_fn([bid_reply(3)]), tmp_path)
        player.decide_bid(make_ai_obs())

        log_entries = [json.loads(line) for line in player.log_path.read_text(encoding="utf-8").splitlines()]
        assert log_entries[0]["content"] == bid_reply(3)
        assert json.loads(log_entries[1]["result"]) == {"bid": 3, "super_bid": None}
        md_text = self.read_md(player)
        assert md_text.startswith("# Alpha 对话记录\n")
        assert "## bid" in md_text
        assert "**【user】**" in md_text and "**【assistant】**" in md_text
        assert '```json\n{"bid": 3}\n```' in md_text  # 正文原样收录，不套外层围栏
        assert '> 出价结果：{"bid": 3, "super_bid": null}' in md_text

    def test_error_entry_without_context_keeps_plain_format(self, tmp_path):
        # 不传 context 的发言失败：保持旧的纯 phase 格式
        chat_fn = fake_chat_fn([RuntimeError("timeout"), speech_reply("发言内容")])
        player = make_player(chat_fn, tmp_path)
        player.give_speech("请发言", "opening_speech")

        assert len(chat_fn.calls) == 2
        assert "> 调用失败（opening_speech，第 1 次）：RuntimeError: timeout" in self.read_md(player)

    def test_legacy_parse_failed_entry_rendered(self, tmp_path):
        # 旧场次日志的 parse_failed 条目带 result: 0（当时静默记 0），渲染保持可识别
        player = make_player(fake_chat_fn([bid_reply(3)]), tmp_path)
        player._write_log({"phase": "bid", "parse_failed": True, "result": 0})

        assert "> 出价解析失败，按规则记 0（旧场次条目）" in self.read_md(player)

    def test_legacy_speech_fallback_entry_rendered(self, tmp_path):
        # 旧场次日志的 speech_fallback 条目（fallback 机制已取消），渲染保持可识别
        player = make_player(fake_chat_fn([bid_reply(3)]), tmp_path)
        player._write_log({"phase": "match_4_speech", "kind": "speech_fallback", "result": "垃圾全文"})

        assert "> 发言块解析失败，重试回复全文已作为公开发言" in self.read_md(player)
