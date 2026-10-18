"""模型能力同步的提示保留配置名与出错字段。"""

import json

import pytest

from auction_game.cli.sync_models import build_snapshot, main


@pytest.mark.parametrize("data,expected", [
    ({"official": {"models": []}}, "每个 provider 必须包含 models 对象"),
    ({"official": {"models": {"source": {"id": "wrong", "limit": {}}}}},
     "sample：来源数据格式错误或 id 与配置不一致"),
    ({"official": {"models": {"source": {"id": "source", "limit": {},
                                           "canonical_model_id": "changed"}}}},
     "sample：canonical_model_id 已变更，请核对 models_dev 来源配置"),
])
def test_source_error_locates_model_and_field(data, expected):
    catalog = {"sample": {"models_dev": {"provider": "official", "model": "source",
                                         "canonical_model_id": "expected"}}}
    with pytest.raises(ValueError, match=expected):
        build_snapshot(catalog, data, "synthetic")


def test_preview_explains_default_budget_and_write_option(config_files, tmp_path, capsys):
    source = tmp_path / "public.json"
    source.write_text(json.dumps({"official": {"models": {}}}), encoding="utf-8")
    assert main(["--models", str(config_files[2]), "--input", str(source)]) == 0
    output = capsys.readouterr().out
    assert "未找到配置指定的精确来源" in output
    assert "暂用默认输出预算：" in output
    assert "仅预览；使用 --write 保存模型能力。" in output
