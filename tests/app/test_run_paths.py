"""场次目录冲突与覆盖保护。"""

from datetime import datetime
from types import SimpleNamespace

import pytest

from auction_game.reporting import run_output
from tests.support.factories import TEST_GAME


def test_same_second_runs_get_suffix_and_existing_run_is_not_overwritten(tmp_path, monkeypatch):
    monkeypatch.setattr(run_output, "LOG_DIR", tmp_path)
    monkeypatch.setattr(run_output, "datetime", SimpleNamespace(now=lambda: datetime(2026, 10, 16, 12)))
    first = run_output.make_run_dir("ai")
    sentinel = first / "unchanged.txt"
    sentinel.write_text("keep", encoding="utf-8")
    second = run_output.make_run_dir("ai")
    assert second.name == first.name + "_2" and sentinel.read_text() == "keep"
    snapshot = {"run_type": "offline", "mode": "regular", "game": TEST_GAME.to_dict(),
                "players": [{"name": "甲"}, {"name": "乙"}]}
    run_output.initialize_run(second, snapshot, "rules")
    with pytest.raises(FileExistsError):
        run_output.initialize_run(second, snapshot, "overwrite")
    with pytest.raises(FileExistsError):
        run_output.initialize_run(first, snapshot, "overwrite")
    assert sentinel.read_text() == "keep"
