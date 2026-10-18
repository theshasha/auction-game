"""实际失败、纠错、回滚与总结请求的磁盘无损恢复。"""

from copy import deepcopy
import json

import pytest

from auction_game.reporting.request_records import load_requests, RequestWriter
from tests.support.factories import bid_reply, fake_task_runner, make_player, make_ai_obs
from auction_game.ai.agent_execution import OutputFailedError


def test_actual_capture_rebuild_after_failure_rollback_and_summary(tmp_path):
    runner = fake_task_runner([bid_reply(1), bid_reply("bad"), bid_reply("bad"), bid_reply(2),
                               bid_reply(3), "summary"])
    runner.executor.capture_requests = True
    captured = []
    original = runner.executor._run

    async def run(*args):
        try:
            result = await original(*args)
        except OutputFailedError as error:
            captured.extend(deepcopy(error.requests))
            raise
        captured.extend(deepcopy(result.requests))
        return result

    runner.executor._run = run
    player = make_player(runner, tmp_path)
    player.decide_bid(make_ai_obs(day=2))
    checkpoint = player.checkpoint()
    with pytest.raises(OutputFailedError):
        player.decide_bid(make_ai_obs(day=3))
    player.decide_bid(make_ai_obs(day=3))
    player.rollback(checkpoint)
    player.decide_bid(make_ai_obs(day=4))
    before = deepcopy(player.messages)
    player.final_summary("summary prompt")
    assert player.messages == before
    entries = [json.loads(line) for line in player.log_path.read_text(encoding="utf-8").splitlines()]
    load_requests(tmp_path / "capture.jsonl", entries)
    assert [record for entry in entries for record in entry["requests"]] == captured
    for entry in entries:
        for record in entry["requests"]:
            assert record["parameters"]["output_tools"] == []
            assert record["parameters"]["function_tools"] == []
            assert record["parameters"].get("output_object") is None
            if "response" in record:
                assert all(part["part_kind"] == "text" for part in record["response"]["parts"])
    raw = [part["content"] for entry in entries for record in entry["requests"]
           for part in record.get("response", {}).get("parts", [])]
    assert sum('<decision>\n{"bid": "bad", "super_bid": null}\n</decision>' in text for text in raw) == 2
    assert raw[-1] == "summary"
    rows = [json.loads(line) for line in (tmp_path / "capture.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(row["prefix"] > 0 for row in rows[1:])
    rows[0]["settings"] = "missing-definition"
    (tmp_path / "broken.jsonl").write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    with pytest.raises(ValueError, match="缺少引用"):
        load_requests(tmp_path / "broken.jsonl", entries)


def test_capture_preserves_null_false_and_absent_fields(tmp_path):
    path = tmp_path / "capture.jsonl"
    writer = RequestWriter(path)
    entries, expected = [], []
    for index, value in enumerate(({}, {"option": None}, {"option": False}, {"option": 0}), 1):
        message = {"kind": "request", "parts": [], **value}
        record = {"request": 1, "messages": [message], "settings": value, "parameters": value,
                  "response": {"kind": "response", "parts": [], **value}}
        entry = {"task_id": str(index), "messages": [message, record["response"]], "request_errors": []}
        entries.append(entry)
        expected.append(record)
        writer.append(entry, [record])
    load_requests(path, entries)
    assert [json.dumps(entry["requests"][0], sort_keys=True) for entry in entries] == [
        json.dumps(record, sort_keys=True) for record in expected]
