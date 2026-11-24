"""estimate 命令：按当前配置打印预估，不写任何文件。"""

from auction_game.cli import estimate
from auction_game.configuration import load_configuration
from tests.support.factories import TEST_GAME, rewrite_yaml


def test_prints_estimate_without_writing_files(config_files, teams_file, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(estimate, "load_configuration",
                        lambda **kwargs: load_configuration(*config_files, teams_path=teams_file, **kwargs))
    monkeypatch.setattr(estimate, "load_game_settings", lambda **kwargs: TEST_GAME)
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    assert estimate.main(["--team", "--test-model"]) == 0
    output = capsys.readouterr().out
    assert output.startswith(f"预估花费（团队赛正赛 {TEST_GAME.regular_matches} 局")
    assert "  test-model-c · high（6 席）：无单价\n" in output
    assert {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before


def test_configuration_error_is_one_line(config_files, monkeypatch, capsys):
    rewrite_yaml(config_files[0], lambda data: data.update(llm=None))
    monkeypatch.setattr(estimate, "load_configuration", lambda **kwargs: load_configuration(*config_files, **kwargs))
    assert estimate.main([]) == 1
    printed = capsys.readouterr().out.splitlines()
    assert len(printed) == 1 and printed[0].startswith("[配置错误] ")
