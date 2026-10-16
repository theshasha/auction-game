"""共用场次布局、目录冲突与默认分析类型边界。"""

import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from auction_game.reporting import analyze, run_output
from auction_game.reporting.run_paths import RunPaths
from tests.support.factories import make_log


def test_collision_and_default_analysis_only_valid_ai(tmp_path, monkeypatch):
    monkeypatch.setattr(run_output, "LOG_DIR", tmp_path)
    monkeypatch.setattr(analyze, "LOG_DIR", tmp_path)
    monkeypatch.setattr(run_output, "datetime", SimpleNamespace(now=lambda: datetime(2026, 10, 16, 12)))
    first = run_output.make_run_dir("ai")
    sentinel = first / "unchanged.txt"
    sentinel.write_text("keep", encoding="utf-8")
    second = run_output.make_run_dir("ai")
    assert second.name == first.name + "_2" and sentinel.read_text() == "keep"
    log = make_log()
    for kind in ("offline", "ai-test", "bot"):
        path = run_output.make_run_dir(kind)
        paths = run_output.initialize_run(path, {"run_type": kind, "mode": "regular", "game": log.game.to_dict(),
                       "players": [{"name": name} for name in log.wins]}, "rules")
        paths.match(1).write_text(json.dumps({"match": log.to_dict()}), encoding="utf-8")
        assert analyze.load_match_logs(path)
    with pytest.raises(SystemExit, match="没有有效"):
        analyze.find_latest_ai_run()
    paths = run_output.initialize_run(second, {"run_type": "ai", "mode": "regular", "game": log.game.to_dict(),
                   "players": [{"name": name} for name in log.wins]}, "rules")
    paths.match(1).write_text(json.dumps({"match": log.to_dict()}), encoding="utf-8")
    assert analyze.find_latest_ai_run() == second
    with pytest.raises(FileExistsError):
        run_output.initialize_run(second, {}, "overwrite")
    with pytest.raises(ValueError, match="旧格式"):
        analyze.load_match_logs(first)
    with pytest.raises(FileExistsError):
        run_output.initialize_run(first, {}, "overwrite")
