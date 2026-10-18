"""真实生产 SDK 的默认轻量记录；请求前保存，零收费调用。"""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path

import pytest

from auction_game.ai.agent_execution import CallFailedError
from auction_game.ai.agent_tasks import TaskRunner
from auction_game.ai.ai_player import AIPlayer
from auction_game.reporting.run_output import initialize_run
from auction_game.reporting.tool_records import ToolWriter, protocol_tools
from tests.support.factories import TEST_GAME
from tests.support.wire import Harness, PROTOCOLS, reply


def setup_run(tmp_path):
    paths = initialize_run(tmp_path / "run", {"run_type": "ai", "mode": "regular", "game": TEST_GAME.to_dict(),
                           "timeout_seconds": 3,
                           "players": [{"name": name, "identity_model": {"model": "identity"},
                                        "execution_model": {"protocol": "chat", "model": "execution", "max_tokens": 100,
                                                            "base_url": "https://offline.invalid/v1"}}
                                       for name in ("Alpha", "Bravo")]}, "rules")
    return paths, ToolWriter(paths)


def player_for(paths, writer, h, number=1):
    name = "Alpha" if number == 1 else "Bravo"
    return AIPlayer(name, "persona", "persona", [], ["Alpha", "Bravo"], TaskRunner(h.factory),
                    paths.player(number), game=TEST_GAME, readable_path=paths.player(number, readable=True),
                    tool_writer=writer, player_number=number)


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_actual_http_definition_retry_failure_summary_and_dedup(protocol, tmp_path):
    paths, writer = setup_run(tmp_path)
    assert "尚无请求记录" in paths.tools.read_text(encoding="utf-8")
    h = Harness(protocol, [503, reply(), reply(), 400, reply(tool=False, text="summary")])
    handle = h.factory.transport.handler

    async def check_before_send(request):
        references = paths.tool_requests.read_text(encoding="utf-8").splitlines()
        assert len(references) == len(h.requests) + 1
        reference = json.loads(references[-1])
        saved = json.loads(paths.tool_definitions.read_text(encoding="utf-8"))
        assert saved[reference["definition_id"]]["definition"] == protocol_tools(json.loads(request.content))
        return await handle(request)

    h.factory.transport.handler = check_before_send
    player = player_for(paths, writer, h)
    player.plan_strategy("plan", "opening_plan")
    player.plan_strategy("next plan", "match_1_plan")
    with pytest.raises(CallFailedError):
        player.plan_strategy("failed plan", "match_2_plan")
    assert player.final_summary("summary") == "summary"
    definitions = json.loads(paths.tool_definitions.read_text(encoding="utf-8"))
    refs = [json.loads(line) for line in paths.tool_requests.read_text(encoding="utf-8").splitlines()]
    assert len(refs) == len(h.requests) == 5 and len(definitions) == 2
    assert [(ref["task_id"], ref["request"], ref["attempt"]) for ref in refs] == [
        ("task-0001", 1, 1), ("task-0001", 1, 2), ("task-0002", 1, 1),
        ("task-0003", 1, 1), ("task-0004", 1, 1)]
    for ref, (_, body) in zip(refs, h.requests):
        assert definitions[ref["definition_id"]] == {"protocol": protocol, "definition": protocol_tools(body)}
        assert ref["source"] == "http_pre_send" and ref["versions"]["openai"]
    entries = [json.loads(line) for line in paths.player(1).read_text(encoding="utf-8").splitlines()]
    assert [ref["definition_id"] for ref in refs[1:]] == [e["tool_definitions"][0] for e in entries]
    text = paths.tools.read_text(encoding="utf-8")
    assert "发送前实际请求定义" in text and "无输出工具" in text
    assert "offline_sdk_generated" in text and "无真实服务调用" in text
    for ref in refs:
        assert f'<a id="{ref["definition_id"]}"></a>' in text
        assert (f'`{ref["task_id"]}` · {ref["task"]} / {ref["phase"]} / {ref["context"]} · '
                f'request {ref["request"]} / attempt {ref["attempt"]} · '
                f'{ref["source"]} · `{ref["model"]}`') in text
    assert text.count('"name": "bid"') == 1
    assert "[本场工具定义](tools.md)" in (paths.root / "README.md").read_text(encoding="utf-8")
    readable = paths.player(1, readable=True).read_text(encoding="utf-8")
    assert "tools.md" not in readable and "task-0001" not in readable
    for path in (paths.tools, paths.tool_definitions, paths.tool_requests):
        data = path.read_text(encoding="utf-8")
        assert not any(secret in data for secret in ("fake-key", '"messages"', '"input"', '"authorization"'))


def test_parallel_players_share_definitions_without_losing_references(tmp_path):
    paths, writer = setup_run(tmp_path)
    players = [player_for(paths, writer, Harness("chat", [reply()]), i) for i in (1, 2)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda player: player.plan_strategy("plan", "opening_plan"), players))
    assert len(json.loads(paths.tool_definitions.read_text(encoding="utf-8"))) == 1
    refs = [json.loads(line) for line in paths.tool_requests.read_text(encoding="utf-8").splitlines()]
    assert {ref["player"] for ref in refs} == {"Alpha", "Bravo"}


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_tool_write_error_propagates_before_network_without_retry(protocol, tmp_path, monkeypatch):
    paths, writer = setup_run(tmp_path)
    h = Harness(protocol, [reply()])
    player = player_for(paths, writer, h)
    before = deepcopy(player.messages)
    original_open = Path.open
    failure = OSError("tool audit disk failure")

    def open_file(path, mode="r", *args, **kwargs):
        if path == paths.tool_requests and mode == "a":
            raise failure
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_file)
    with pytest.raises(OSError) as caught:
        player.plan_strategy("plan", "opening_plan")
    assert caught.value is failure and not h.requests
    assert player.messages == before and player._pending_day1_bid is None and h.closes == 1


def test_tool_field_extraction_keeps_absence_null_and_false():
    assert protocol_tools({}) == {}
    assert protocol_tools({"tools": None, "tool_choice": None, "parallel_tool_calls": False,
                           "messages": ["secret"]}) == {"tools": None, "tool_choice": None, "parallel_tool_calls": False}
