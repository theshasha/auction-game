"""AI 玩家的测试：出价解析、对话线只增不改、md 双写日志。全部注入 fake chat_fn，零网络。
发言与规划合并见 test_ai_speech.py；控制器/并发执行器见 test_controller.py；
共享夹具见 conftest.py。"""

import json

import pytest

from ai_player import CallFailedError, InvalidBidError, ParseFailedError
from conftest import bid_reply, fake_chat_fn, make_ai_obs, make_player, speech_reply


class TestBidParsing:
    def test_normal_reply_parsed(self):
        player = make_player(fake_chat_fn([bid_reply(3)]))
        assert player.decide_bid(make_ai_obs()) == (3, None)

    def test_super_bid_parsed(self):
        # 同一 JSON 对象内带可选 super_bid 字段：一次提交两笔出价
        reply = '分析：……\n{"bid": 4, "super_bid": 6}'
        player = make_player(fake_chat_fn([reply]))
        assert player.decide_bid(make_ai_obs()) == (4, 6)

    def test_null_super_bid_treated_as_unused(self):
        reply = '分析：……\n{"bid": 4, "super_bid": null}'
        player = make_player(fake_chat_fn([reply]))
        assert player.decide_bid(make_ai_obs()) == (4, None)

    def test_uses_last_json_block(self):
        content = "```json\n{\"bid\": 1}\n```\n改主意了\n```json\n{\"bid\": 4}\n```"
        player = make_player(fake_chat_fn([content]))
        assert player.decide_bid(make_ai_obs()) == (4, None)

    def test_bare_json_without_fence_parsed(self):
        # 提示词示例无围栏，模型直接输出裸 JSON 时必须能解析（实证：deepseek-v4-pro 曾裸输出）
        player = make_player(fake_chat_fn(["我的分析：……\n{\"bid\": 2}"]))
        assert player.decide_bid(make_ai_obs()) == (2, None)

    def test_bare_json_uses_last_valid_object(self):
        content = "先举个例子 {\"bid\": 1}，但改主意了\n{\"bid\": 5}"
        player = make_player(fake_chat_fn([content]))
        assert player.decide_bid(make_ai_obs()) == (5, None)

    def test_bare_json_last_object_without_bid_key_fails(self):
        # 最后一个合法 JSON 无 bid 键：不再向前找，按解析失败处理（重试 1 次后成功）
        script = ["正文引用了 {\"note\": \"x\"} 但没出价", bid_reply(3)]
        chat_fn = fake_chat_fn(script)
        player = make_player(chat_fn)
        assert player.decide_bid(make_ai_obs()) == (3, None)
        assert len(chat_fn.calls) == 2

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
        assert len(player.messages) == 1
        log_entries = [
            json.loads(line) for line in player.log_path.read_text(encoding="utf-8").splitlines()
        ]
        assert sum(1 for entry in log_entries if "EmptyResponseError" in entry.get("error", "")) == 2

    def test_invalid_value_retried_with_reason(self):
        # 数值非法（超上限）：提醒重试并写清违规原因，第二次合法即通过
        chat_fn = fake_chat_fn([bid_reply(9), bid_reply(6)])
        player = make_player(chat_fn)
        assert player.decide_bid(make_ai_obs()) == (6, None)
        assert len(chat_fn.calls) == 2
        retry_user_msg = player.messages[-2]["content"]
        assert "上一提交非法" in retry_user_msg
        assert "超出 0~7 的范围" in retry_user_msg

    def test_bid_over_balance_retried_with_reason(self):
        # 超余额：提醒文案包含具体余额
        chat_fn = fake_chat_fn([bid_reply(6), bid_reply(3)])
        player = make_player(chat_fn)
        assert player.decide_bid(make_ai_obs(coins=3)) == (3, None)
        retry_user_msg = player.messages[-2]["content"]
        assert "超过当前余额 3" in retry_user_msg

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

    def test_super_bid_total_cost_over_balance_is_invalid(self):
        # 正常出价 + 超级成本 2 金币合计超余额（v6 整单口径）：提醒重试，
        # 第二次去掉 super_bid 即通过
        chat_fn = fake_chat_fn(['{"bid": 3, "super_bid": 6}', bid_reply(3)])
        player = make_player(chat_fn)
        assert player.decide_bid(make_ai_obs(coins=4)) == (3, None)
        retry_user_msg = player.messages[-2]["content"]
        assert "合计超过当前余额 4" in retry_user_msg

    def test_super_bid_equal_to_normal_is_invalid(self):
        # 同值属非法提交（v6：整单作废）：提醒重试，第二次去掉 super_bid 即通过
        chat_fn = fake_chat_fn(['{"bid": 3, "super_bid": 3}', bid_reply(3)])
        player = make_player(chat_fn)
        assert player.decide_bid(make_ai_obs()) == (3, None)
        assert "不能等于你的正常出价" in player.messages[-2]["content"]

    def test_super_bid_out_of_range_is_invalid(self):
        chat_fn = fake_chat_fn(['{"bid": 3, "super_bid": 9}', bid_reply(3)])
        player = make_player(chat_fn)
        assert player.decide_bid(make_ai_obs()) == (3, None)
        assert "超级出价 9 超出 0~7 的范围" in player.messages[-2]["content"]

    def test_error_then_success_recovers(self):
        # 第一次调用失败（如网络抖动）立即重试，成功后正常出价
        chat_fn = fake_chat_fn([RuntimeError("timeout"), bid_reply(4)])
        player = make_player(chat_fn)
        assert player.decide_bid(make_ai_obs()) == (4, None)
        assert len(chat_fn.calls) == 2
        # 失败的 user 消息已回滚，对话线只有 system + 成功的一问一答
        assert [m["role"] for m in player.messages] == ["system", "user", "assistant"]

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
        script = [bid_reply(1), bid_reply(2), bid_reply(3)]
        player = make_player(fake_chat_fn(script))
        prefix_snapshot = list(player.messages)
        player.decide_bid(make_ai_obs(day=1))
        mid_snapshot = list(player.messages)
        player.decide_bid(make_ai_obs(day=2))
        # 任意时刻已有消息逐字节不变
        assert player.messages[: len(prefix_snapshot)] == prefix_snapshot
        assert player.messages[: len(mid_snapshot)] == mid_snapshot
        assert player.messages[0]["role"] == "system"

    def test_content_logged_to_jsonl(self, tmp_path):
        player = make_player(fake_chat_fn([bid_reply(3)]), tmp_path)
        player.decide_bid(make_ai_obs())

        log_entries = [json.loads(line) for line in player.log_path.read_text(encoding="utf-8").splitlines()]
        assert any(entry.get("content") == bid_reply(3) for entry in log_entries)

    def test_rollback_truncates_only_tail(self):
        player = make_player(fake_chat_fn([bid_reply(1), bid_reply(2)]))
        player.decide_bid(make_ai_obs(day=1))
        checkpoint = player.checkpoint()
        prefix = list(player.messages)
        player.decide_bid(make_ai_obs(day=2))
        player.rollback(checkpoint)
        assert player.messages == prefix


class TestMarkdownLog:
    """md 双写：每条 jsonl 日志同步渲染追加到同名 .md（jsonl 断言见既有用例，不动）。"""

    @staticmethod
    def read_md(player) -> str:
        return player.log_path.with_suffix(".md").read_text(encoding="utf-8")

    def test_normal_bid_entry_rendered(self, tmp_path):
        player = make_player(fake_chat_fn([bid_reply(3)]), tmp_path)
        player.decide_bid(make_ai_obs())

        md_text = self.read_md(player)
        assert md_text.startswith("# Alpha 对话记录\n")
        assert "## bid" in md_text
        assert "**【user】**" in md_text and "**【assistant】**" in md_text
        assert '```json\n{"bid": 3}\n```' in md_text  # 正文原样收录，不套外层围栏
        assert '> 出价结果：{"bid": 3, "super_bid": null}' in md_text

    def test_error_entry_rendered(self, tmp_path):
        # 第一次调用失败重试成功：md 含失败备注，且最终问答仍完整
        chat_fn = fake_chat_fn([RuntimeError("timeout"), bid_reply(4)])
        player = make_player(chat_fn, tmp_path)
        assert player.decide_bid(make_ai_obs()) == (4, None)

        md_text = self.read_md(player)
        assert "> 调用失败（bid，第1局第1天，第 1 次）：RuntimeError: timeout" in md_text
        assert "## bid" in md_text

    def test_error_entry_without_context_keeps_plain_format(self, tmp_path):
        # 不传 context 的发言失败：保持旧的纯 phase 格式
        chat_fn = fake_chat_fn([RuntimeError("timeout"), speech_reply("发言内容")])
        player = make_player(chat_fn, tmp_path)
        player.give_speech("请发言", "opening_speech")

        assert "> 调用失败（opening_speech，第 1 次）：RuntimeError: timeout" in self.read_md(player)

    def test_parse_failed_entry_rendered(self, tmp_path):
        player = make_player(fake_chat_fn(["没有块", "还是没有"]), tmp_path)
        with pytest.raises(ParseFailedError):
            player.decide_bid(make_ai_obs())

        assert "> bid：解析连续失败，已挂起等待人工重跑" in self.read_md(player)

    def test_legacy_parse_failed_entry_rendered(self, tmp_path):
        # 旧场次日志的 parse_failed 条目带 result: 0（当时静默记 0），渲染保持可识别
        player = make_player(fake_chat_fn([bid_reply(3)]), tmp_path)
        player._write_log({"phase": "bid", "parse_failed": True, "result": 0})

        assert "> 出价解析失败，按规则记 0（旧场次条目）" in self.read_md(player)

    def test_speech_result_entry_rendered(self, tmp_path):
        player = make_player(fake_chat_fn([speech_reply("公开内容")]), tmp_path)
        player.give_speech("请发言", "opening_speech")

        assert "> 公开发言内容：公开内容" in self.read_md(player)

    def test_legacy_speech_fallback_entry_rendered(self, tmp_path):
        # 旧场次日志的 speech_fallback 条目（fallback 机制已取消），渲染保持可识别
        player = make_player(fake_chat_fn([bid_reply(3)]), tmp_path)
        player._write_log({"phase": "match_4_speech", "kind": "speech_fallback", "result": "垃圾全文"})

        assert "> 发言块解析失败，重试回复全文已作为公开发言" in self.read_md(player)
