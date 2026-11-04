"""比赛入口的参数分派。"""

import sys

import pytest

import main
from auction_game import runners


@pytest.mark.parametrize("args,test_model,team", [
    ([], False, False), (["--test-model"], True, False), (["--team"], False, True), (["--team", "--test-model"], True, True),
])
@pytest.mark.parametrize("status", [0, 1])
def test_main_dispatches_to_ai_run(monkeypatch, args, test_model, team, status):
    calls = []
    monkeypatch.setattr(runners, "run_ai", lambda **kwargs: calls.append(kwargs) or status)
    monkeypatch.setattr(sys, "argv", ["main.py", *args])
    assert main.main() == status
    assert calls == [{"test_model": test_model, "team": team}]


@pytest.mark.parametrize("args", [["--unknown"], ["extra"]])
def test_main_rejects_unknown_arguments_before_dispatch(monkeypatch, args):
    monkeypatch.setattr(runners, "run_ai", lambda **kwargs: pytest.fail("参数错误时不应启动比赛"))
    monkeypatch.setattr(sys, "argv", ["main.py", *args])
    with pytest.raises(SystemExit) as exc:
        main.main()
    assert exc.value.code == 2
