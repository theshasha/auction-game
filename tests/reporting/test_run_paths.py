"""场次目录冲突与覆盖保护。"""

from datetime import datetime
from types import SimpleNamespace

import pytest

from auction_game.reporting import run_paths
from auction_game.reporting.run_output import initialize_run
from tests.support.factories import TEST_GAME


def test_same_second_runs_get_suffix_and_existing_run_is_not_overwritten(tmp_path, monkeypatch):
    monkeypatch.setattr(run_paths, "LOG_DIR", tmp_path)
    monkeypatch.setattr(run_paths, "datetime", SimpleNamespace(now=lambda: datetime(2026, 10, 16, 12)))
    first = run_paths.make_run_dir("ai")
    sentinel = first / "unchanged.txt"
    sentinel.write_text("keep", encoding="utf-8")
    second = run_paths.make_run_dir("ai")
    assert second.name == first.name + "_2" and sentinel.read_text() == "keep"
    snapshot = {"run_type": "offline", "mode": "regular", "game": TEST_GAME.to_dict(),
                "players": [{"name": "甲", "team": None}, {"name": "乙", "team": None}]}
    initialize_run(second, snapshot, "rules")
    with pytest.raises(FileExistsError):
        initialize_run(second, snapshot, "overwrite")
    with pytest.raises(FileExistsError):
        initialize_run(first, snapshot, "overwrite")
    assert sentinel.read_text() == "keep"


@pytest.mark.parametrize("team,test_model", [(False, False), (False, True), (True, False), (True, True)])
def test_runs_made_for_each_real_type_are_found_newest_last(tmp_path, monkeypatch, team, test_model):
    monkeypatch.setattr(run_paths, "LOG_DIR", tmp_path)
    run_type = run_paths.run_type(team=team, test_model=test_model)
    times = iter([datetime(2026, 10, 16, 12)] * 2 + [datetime(2026, 10, 15, 12), datetime(2026, 10, 17, 12)])
    monkeypatch.setattr(run_paths, "datetime", SimpleNamespace(now=lambda: next(times)))
    first, second, older = (run_paths.make_run_dir(run_type) for _ in range(3))
    run_paths.make_run_dir(run_paths.run_type(team=team, offline=True))
    (tmp_path / f"archive_{first.name}").mkdir()
    assert run_paths.find_runs(run_paths.REAL_RUN_TYPES) == [older, first, second]
