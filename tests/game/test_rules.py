"""规则模板生成与参数贯通。"""

from dataclasses import replace
import sys

import pytest

from auction_game.agent import prompts
from auction_game.game import rules
from auction_game.cli import rules as rules_command
from auction_game.configuration import load_game_settings, load_players
from tests.support.factories import NAMES, TEST_GAME
from auction_game.game.rules import render_rules


class TestRulesText:
    """规则正文源于模板，生成文档与 system 使用相同渲染函数。"""

    def test_generated_rules_matches_configuration(self, tmp_path, monkeypatch):
        names = [player["name"] for player in load_players()]
        text = render_rules(load_game_settings(), names)
        document = tmp_path / "RULES.md"
        monkeypatch.setattr(rules_command, "RULES_PATH", document)
        rules_command.sync_root_rules(load_game_settings())
        assert document.read_text(encoding="utf-8") == text
        system = prompts.build_system_prompt(names[0], "人设", "正文", [], names, load_game_settings())
        body = render_rules(load_game_settings(), names, include_note=False)
        assert text == rules.GENERATED_NOTE + body
        assert system.startswith(body)
        assert rules.GENERATED_NOTE not in system

    def test_body_omits_only_generation_note(self):
        body = render_rules(TEST_GAME, NAMES, include_note=False)
        assert body.startswith("【唯一高价拍卖游戏规则】")
        assert "<!--" not in body
        assert render_rules(TEST_GAME, NAMES) == rules.GENERATED_NOTE + body

    def test_custom_parameters_and_actual_names_reach_system(self):
        game = replace(
            TEST_GAME, initial_coins=11, daily_salary=4, daily_pool=(2, 1, 3),
            max_bid=9, super_cost=3, super_bounty=5, super_hit_score=6,
            regular_matches=2, max_playoffs=4,
        )
        names = ["甲", "乙", "丙"]
        text = render_rules(game, names, include_note=False)
        assert render_rules(game, names) == rules.GENERATED_NOTE + text
        assert "3 名玩家参赛（甲、乙、丙）" in text
        assert "共 2 局正赛，每局 3 天" in text
        assert "第 1 天 2 分、第 2 天 1 分、第 3 天 3 分" in text
        assert "每局奖池合计 6 分" in text
        assert "每人有 11 枚金币" in text and "每人再得 4 枚" in text
        assert "0~9 的整数" in text and "固定花费 3 金币" in text
        assert "获得 5 金币悬赏和 6 命中分" in text
        assert "最多加赛 4 局" in text
        assert "{" not in text and "}" not in text
        system = prompts.build_system_prompt("甲", "人设", "正文", [], names, game)
        assert system.startswith(text)
        assert "其他 2 位玩家是：乙、丙" in system

    def test_unknown_placeholder_raises(self, tmp_path, monkeypatch):
        template = tmp_path / "RULES.template.md"
        template.write_text("{misspelled_parameter}", encoding="utf-8")
        monkeypatch.setattr(rules, "TEMPLATE_PATH", template)
        with pytest.raises(KeyError, match="misspelled_parameter"):
            render_rules(TEST_GAME, NAMES)

    def test_runtime_render_does_not_read_generated_document(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rules_command, "RULES_PATH", tmp_path / "missing.md")
        text = render_rules(TEST_GAME, NAMES, include_note=False)
        system = prompts.build_system_prompt(
            "甲", "人设", "正文", [], NAMES, TEST_GAME
        )
        assert system.startswith(text)

    def test_check_command_rejects_stale_document_and_keeps_it(self, tmp_path, monkeypatch):
        document = tmp_path / "RULES.md"
        document.write_text("过期规则", encoding="utf-8")
        monkeypatch.setattr(rules_command, "RULES_PATH", document)
        monkeypatch.setattr(sys, "argv", ["rules.py", "--check"])
        assert rules_command.main() == 1
        assert document.read_text(encoding="utf-8") == "过期规则"
