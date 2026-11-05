"""模型能力同步的预览提示。"""

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
