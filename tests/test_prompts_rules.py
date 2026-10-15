"""规则模板生成、参数贯通与局末重申。共享工厂见 conftest.py。"""

from dataclasses import replace
from pathlib import Path
import subprocess
import sys

import pytest

import prompts
import rules
from configuration import load_game_settings, load_players
from conftest import NAMES, TEST_GAME, make_log, make_standings
from rules import render_rules


class TestRulesReminder:
    """局末发言请求在展示局日志前重申结算三步（精简版）。"""

    def test_match_end_speech_contains_reminder_before_log(self):
        text = prompts.match_end_speech_request(
            make_log(), make_standings({n: [0] for n in NAMES}), True
        )
        reminder = prompts.rules_reminder(TEST_GAME)
        assert reminder in text
        assert text.index(reminder) < text.index("本局完整出价日志")

    def test_reminder_covers_three_steps_and_super_bid(self):
        reminder = prompts.rules_reminder(TEST_GAME)
        assert "两笔及以上正常出价即撞车" in reminder
        assert "未撞车的正常出价中最高的一笔获胜" in reminder
        assert "全额退还" in reminder
        # 超级出价要点（收费狙击制）：不限次数、每次固定扣 2 金币、只狙击不能获胜、
        # 命中（≥1 笔正常出价）+3 悬赏、出价明细与全场余额每日公布
        assert "超级出价不限次数（每天最多 1 笔）" in reminder
        assert "固定扣除 2 金币" in reminder
        assert "不能获胜" in reminder
        assert "3 金币悬赏" in reminder
        assert "至少有 1 笔正常出价" in reminder
        assert "昨日完整出价明细" in reminder
        assert "全场每人的当前金币余额" in reminder


class TestRulesText:
    """规则正文源于模板，生成文档与 system 使用相同渲染函数。"""

    def test_generated_rules_matches_configuration(self):
        names = [player["name"] for player in load_players()]
        text = render_rules(load_game_settings(), names)
        assert rules.RULES_PATH.read_text(encoding="utf-8") == text
        system = prompts.build_system_prompt(names[0], "人设", "正文", [], names, load_game_settings())
        assert system.startswith(text)

    def test_core_mechanics_present(self):
        text = render_rules(TEST_GAME, NAMES)
        assert "每局 6 天" in text
        assert "每天拍卖 1 个胜场，每局共 6 个胜场" in text
        assert "并入次日奖池" in text
        assert "每天开始时每人发放 3 枚金币" in text
        assert "0~7的整数" in text

    def test_super_bid_rules_present(self):
        text = render_rules(TEST_GAME, NAMES)
        # 超级出价是收费狙击工具——不限次数（每天最多 1 笔）、
        # 固定消耗 2 金币、与正常出价合计不超余额、不能等于当天正常出价、
        # 不能获胜；命中（≥1 笔正常出价）发 3 金币悬赏，多人命中同值各得 3
        assert "可额外选择提交最多 1 笔超级出价" in text
        assert "提交超级出价将固定消耗 2 枚金币" in text
        assert "当天的超级出价数值不得等于正常出价数值" in text
        assert "两者总和不得超过玩家当前金币余额" in text
        assert "绝对不参与获胜判定" in text
        assert "系统向该狙击者发放 3 金币悬赏" in text
        assert "每位狙击者均独立获得 3 金币悬赏" in text

    def test_settlement_three_steps_present(self):
        text = render_rules(TEST_GAME, NAMES)
        assert "判定撞车" in text
        assert "判定胜负" in text
        assert "金币结算" in text
        assert "未获胜且未撞车的正常出价全额退还" in text
        # 流局判定：当天正常出价全部撞车则无人获胜
        assert "若当天所有正常出价均撞车" in text
        assert "宣告流局" in text

    def test_publication_includes_yesterday_log_and_all_coins(self):
        # 规则四.1：第 2 天起公布昨日完整出价明细与全场每人当前余额
        text = render_rules(TEST_GAME, NAMES)
        assert "昨日出价日志" in text
        assert "具体出价明细" in text
        assert "全场每位玩家当前的总金币数" in text
        assert "每局结束后，公开该局完整出价日志" in text

    def test_custom_parameters_and_actual_names_reach_system(self):
        game = replace(
            TEST_GAME, initial_coins=11, daily_salary=4, daily_pool=(2, 1, 3),
            max_bid=9, super_cost=3, super_bounty=5, regular_matches=2, max_playoffs=4,
        )
        names = ["甲", "乙", "丙"]
        text = render_rules(game, names)
        assert "3 名玩家参赛（甲、乙、丙）" in text
        assert "共 2 局正赛，每局 3 天" in text
        assert "第 1 天 2 个胜场、第 2 天 1 个胜场、第 3 天 3 个胜场" in text
        assert "每局计划共 6 个胜场" in text
        assert "发放 11 枚金币" in text and "发放 4 枚金币" in text
        assert "0~9的整数" in text and "固定消耗 3 枚金币" in text
        assert "发放 5 金币悬赏" in text
        assert "最多进行 4 局加赛" in text
        assert "保留并列冠军" in text
        assert "第 3 天未拍出的胜场作废" in text
        assert "超级出价数值仅为狙击目标，不计入支付金额" in text
        assert "{" not in text and "}" not in text
        system = prompts.build_system_prompt("甲", "人设", "正文", [], names, game)
        assert system.startswith(text)
        assert "其他 2 位玩家是：乙、丙" in system
        reminder = prompts.rules_reminder(game)
        assert "固定扣除 3 金币" in reminder and "5 金币悬赏" in reminder
        assert "第 3 天流局时奖池作废" in reminder
        assert "当天正常出价加固定成本 3 金币不得超过你的余额" in reminder

    @pytest.mark.parametrize("names", [[], ["甲"], ["甲", "甲"], ["甲", " "]])
    def test_invalid_actual_names_rejected(self, names):
        with pytest.raises(ValueError):
            render_rules(TEST_GAME, names)

    def test_unknown_placeholder_is_not_silently_preserved(self, tmp_path, monkeypatch):
        template = tmp_path / "RULES.template.md"
        template.write_text("{misspelled_parameter}", encoding="utf-8")
        monkeypatch.setattr(rules, "TEMPLATE_PATH", template)
        with pytest.raises(KeyError, match="misspelled_parameter"):
            render_rules(TEST_GAME, NAMES)

    def test_runtime_render_does_not_read_generated_document(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rules, "RULES_PATH", tmp_path / "missing.md")
        text = render_rules(TEST_GAME, NAMES)
        system = prompts.build_system_prompt(
            "甲", "人设", "正文", [], NAMES, TEST_GAME
        )
        assert system.startswith(text)

    def test_check_command_works_outside_project_without_writing(self, tmp_path):
        before = rules.RULES_PATH.read_bytes()
        result = subprocess.run(
            [sys.executable, "-B", str(Path(rules.__file__).resolve()), "--check"],
            cwd=tmp_path, capture_output=True,
        )
        assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
        assert rules.RULES_PATH.read_bytes() == before

    def test_check_command_rejects_stale_document_without_writing(self, tmp_path, monkeypatch):
        document = tmp_path / "RULES.md"
        document.write_text("过期规则", encoding="utf-8")
        monkeypatch.setattr(rules, "RULES_PATH", document)
        monkeypatch.setattr(sys, "argv", ["rules.py", "--check"])
        assert rules.main() == 1
        assert document.read_text(encoding="utf-8") == "过期规则"

    def test_check_command_rejects_bad_template(self, tmp_path, monkeypatch):
        template = tmp_path / "RULES.template.md"
        template.write_text("{unknown}", encoding="utf-8")
        monkeypatch.setattr(rules, "TEMPLATE_PATH", template)
        monkeypatch.setattr(sys, "argv", ["rules.py", "--check"])
        assert rules.main() == 1
