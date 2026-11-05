"""规则模板生成与参数贯通。"""

from dataclasses import replace
import re
import sys

import pytest

from auction_game.agent import prompts
from auction_game.game import rules
from auction_game.cli import rules as rules_command
from auction_game.configuration import load_game_settings, load_players, load_teams
from auction_game.game.lineup import Lineup
from auction_game.paths import TEAM_TEMPLATE_PATH, TEMPLATE_PATH
from tests.support.factories import TEST_GAME, make_lineup, make_team_lineup
from auction_game.game.rules import render_rules


def sections(path):
    """按"一、"到"五、"的节标题切开模板，返回节序号到整节原文。"""
    parts = re.split(r"^(?=[一二三四五]、)", path.read_text(encoding="utf-8"), flags=re.MULTILINE)
    return {part[0]: part for part in parts[1:]}


class TestRulesText:
    """规则正文源于模板，生成文档与 system 使用相同渲染函数。"""

    @pytest.mark.parametrize("team", [False, True])
    def test_generated_rules_matches_configuration(self, tmp_path, monkeypatch, team):
        lineup = Lineup(tuple(profile for profile, _ in (load_teams() if team else load_players())))
        text = render_rules(load_game_settings(), lineup)
        document = tmp_path / "RULES.md"
        monkeypatch.setattr(rules_command, "TEAM_RULES_PATH" if team else "RULES_PATH", document)
        rules_command.sync_root_rules(load_game_settings(), team=team)
        assert document.read_text(encoding="utf-8") == text
        system = prompts.build_system_prompt(lineup.profiles[0], lineup, load_game_settings())
        body = render_rules(load_game_settings(), lineup, include_note=False)
        note = rules.TEAM_GENERATED_NOTE if team else rules.GENERATED_NOTE
        assert text == note + body
        assert system.startswith(body)
        assert note not in system
        assert "{" not in body and "}" not in body

    def test_body_omits_only_generation_note(self):
        body = render_rules(TEST_GAME, make_lineup(), include_note=False)
        assert body.startswith("【唯一高价拍卖游戏规则】")
        assert "<!--" not in body
        assert render_rules(TEST_GAME, make_lineup()) == rules.GENERATED_NOTE + body

    def test_custom_parameters_and_actual_names_reach_system(self):
        game = replace(
            TEST_GAME, initial_coins=11, daily_salary=4, daily_pool=(2, 1, 3),
            max_bid=9, super_cost=3, super_bounty=5, super_hit_score=6,
            regular_matches=2, max_playoffs=4,
        )
        lineup = make_lineup(["甲", "乙", "丙"])
        text = render_rules(game, lineup, include_note=False)
        assert render_rules(game, lineup) == rules.GENERATED_NOTE + text
        assert "3 名玩家参赛（甲、乙、丙）" in text
        assert "共 2 局正赛，每局 3 天" in text
        assert "第 1 天 2 分、第 2 天 1 分、第 3 天 3 分" in text
        assert "每局奖池合计 6 分" in text
        assert "每人有 11 枚金币" in text and "每人再得 4 枚" in text
        assert "0~9 的整数" in text and "固定花费 3 金币" in text
        assert "获得 5 金币悬赏和 6 命中分" in text
        assert "最多加赛 4 局" in text
        assert "{" not in text and "}" not in text
        system = prompts.build_system_prompt(lineup.profiles[0], lineup, game)
        assert system.startswith(text)
        assert "其他 2 位玩家是：乙、丙" in system

    def test_team_rules_list_teams_and_reach_system(self):
        game = replace(TEST_GAME, super_cost=3, super_bounty=5, super_hit_score=6, max_playoffs=4)
        lineup = make_team_lineup()
        text = render_rules(game, lineup, include_note=False)
        assert render_rules(game, lineup) == rules.TEAM_GENERATED_NOTE + text
        assert "6 名玩家分成两个阵营参赛，团队赛：红队有 甲、乙、丙，蓝队有 丁、戊、己。" in text
        assert "阵营总分是 3 名队员的总分之和" in text
        assert "最多加赛 4 局，到上限仍同分时算平局" in text
        assert "乙、戊、己各付 3 金币；戊、己各得 5 金币悬赏和 6 命中分" in text
        assert "{" not in text and "}" not in text
        assert prompts.build_system_prompt(lineup.profiles[0], lineup, game).startswith(text)

    def test_team_template_shares_bidding_section(self):
        assert sections(TEAM_TEMPLATE_PATH)["三"] == sections(TEMPLATE_PATH)["三"]

    def test_team_template_shares_information_and_speech_rules(self):
        """第五节第 1–3 条相同；之后是开局讨论、记忆和队内信件区，团队赛另写。"""
        team, individual = (sections(path)["五"].splitlines()[1:4] for path in (TEAM_TEMPLATE_PATH, TEMPLATE_PATH))
        assert team == individual and team[-1].startswith("3. ")

    def test_team_rules_state_discussion_rounds(self):
        assert "每个阵营进行 3 轮队内讨论，称为开局讨论" in render_rules(TEST_GAME, make_team_lineup())

    @pytest.mark.parametrize("attribute,lineup", [("TEMPLATE_PATH", make_lineup()),
                                                  ("TEAM_TEMPLATE_PATH", make_team_lineup())])
    def test_unknown_placeholder_raises(self, tmp_path, monkeypatch, attribute, lineup):
        template = tmp_path / "template.md"
        template.write_text("{misspelled_parameter}", encoding="utf-8")
        monkeypatch.setattr(rules, attribute, template)
        with pytest.raises(KeyError, match="misspelled_parameter"):
            render_rules(TEST_GAME, lineup)

    def test_runtime_render_does_not_read_generated_document(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rules_command, "RULES_PATH", tmp_path / "missing.md")
        lineup = make_lineup()
        text = render_rules(TEST_GAME, lineup, include_note=False)
        system = prompts.build_system_prompt(lineup.profiles[0], lineup, TEST_GAME)
        assert system.startswith(text)

    def test_check_command_rejects_stale_document_and_keeps_it(self, tmp_path, monkeypatch):
        document = tmp_path / "RULES.md"
        document.write_text("过期规则", encoding="utf-8")
        monkeypatch.setattr(rules_command, "RULES_PATH", document)
        monkeypatch.setattr(sys, "argv", ["rules.py", "--check"])
        assert rules_command.main() == 1
        assert document.read_text(encoding="utf-8") == "过期规则"
