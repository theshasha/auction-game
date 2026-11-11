"""比赛入口的参数分派。"""

from pathlib import Path
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


@pytest.mark.parametrize("args,run_dir", [(["--resume"], None), (["--resume", "logs/某一场"], Path("logs/某一场"))])
@pytest.mark.parametrize("status", [0, 1])
def test_main_dispatches_resume_with_optional_run_directory(monkeypatch, args, run_dir, status):
    calls = []
    monkeypatch.setattr(runners, "run_ai", lambda **kwargs: pytest.fail("续跑不应新开比赛"))
    monkeypatch.setattr(runners, "resume_ai", lambda run_dir: calls.append(run_dir) or status)
    monkeypatch.setattr(sys, "argv", ["main.py", *args])
    assert main.main() == status
    assert calls == [run_dir]


@pytest.mark.parametrize("args", [["--unknown"], ["extra"], ["--resume", "--team"], ["--test-model", "--resume", "x"]])
def test_main_rejects_unknown_arguments_before_dispatch(monkeypatch, args):
    monkeypatch.setattr(runners, "run_ai", lambda **kwargs: pytest.fail("参数错误时不应启动比赛"))
    monkeypatch.setattr(runners, "resume_ai", lambda run_dir: pytest.fail("参数错误时不应续跑"))
    monkeypatch.setattr(sys, "argv", ["main.py", *args])
    with pytest.raises(SystemExit) as exc:
        main.main()
    assert exc.value.code == 2
