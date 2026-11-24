"""模型能力同步的预览提示和写出的来源。"""

import json

from auction_game.cli.sync_models import main


def test_preview_explains_default_budget_and_write_option(config_files, tmp_path, capsys):
    source = tmp_path / "public.json"
    source.write_text(json.dumps({"official": {"models": {}}}), encoding="utf-8")
    assert main(["--models", str(config_files[2]), "--input", str(source)]) == 0
    output = capsys.readouterr().out
    assert "未找到配置指定的精确来源" in output
    assert "暂用默认输出预算：" in output
    assert "仅预览；使用 --write 保存模型能力。" in output


def test_local_input_records_only_file_name(config_files, tmp_path):
    source = tmp_path / "downloads" / "api.json"
    source.parent.mkdir()
    source.write_text(json.dumps({"official": {"models": {}}}), encoding="utf-8")
    output = tmp_path / "snapshot.json"
    assert main(["--models", str(config_files[2]), "--input", str(source), "--output", str(output), "--write"]) == 0
    assert json.loads(output.read_text(encoding="utf-8"))["retrieved_from"] == "api.json"
