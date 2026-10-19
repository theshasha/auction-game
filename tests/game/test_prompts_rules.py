"""规则模板生成、参数贯通与局末事实展示。共享工厂见 conftest.py。"""

from dataclasses import replace
import subprocess
import sys
import os

import pytest

from auction_game.ai import prompts
from auction_game.game import rules
from auction_game.configuration import load_game_settings, load_players
from tests.support.factories import NAMES, TEST_GAME, make_log, make_standings
from auction_game.game.rules import render_rules
from auction_game.paths import ROOT_DIR


class TestSettlementFacts:
    """固定规则只在 system；局末展示实际结果，不再重复结算算法。"""

    def test_match_end_speech_contains_results_without_reminder(self):
        text = prompts.match_end_speech_request(
            make_log(), make_standings({n: [0] for n in NAMES}), True
        )
        assert "规则要点重申" not in text
        assert "本局完整出价日志" in text and "5（获胜）" in text and "4（退款）" in text

    def test_memory_prompt_covers_cumulative_notes_and_other_speeches(self):
        text = prompts.memory_request({"乙": "他人发言"})
        assert "其他玩家在本局结束后的公开发言" in text and "【乙】他人发言" in text
        assert "完整替换旧记忆" in text and "篇幅可以根据有用信息的多少调整" in text
        assert "规则与最新成绩" in text and "无需抄写" in text


class TestRulesText:
    """规则正文源于模板，生成文档与 system 使用相同渲染函数。"""

    def test_generated_rules_matches_configuration(self, tmp_path, monkeypatch):
        names = [player["name"] for player in load_players()]
        text = render_rules(load_game_settings(), names)
        document = tmp_path / "RULES.md"
        monkeypatch.setattr(rules, "RULES_PATH", document)
        rules.sync_root_rules()
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

    def test_core_mechanics_present(self):
        text = render_rules(TEST_GAME, NAMES)
        assert "每局 6 天" in text
        assert f"每天拍卖 {TEST_GAME.daily_pool[0]} 分，每局共 {TEST_GAME.auction_pool_per_match} 分拍卖奖池" in text
        assert "当天的拍卖分作废，不计入后续奖池。比赛不延长。" in text
        assert "每局开始时每人发放 7 枚金币" in text
        assert "第 2 天起每天开始时每人发放 3 枚金币" in text
        assert "本局结束后金币清零，下一局重新发放" in text
        assert "0~7 的整数" in text

    def test_super_bid_rules_present(self):
        text = render_rules(TEST_GAME, NAMES)
        assert "1 笔正常出价" in text and "可再提交 1 笔超级出价" in text
        assert "数值必须与自己的正常出价不同" in text
        assert "每次固定花费 2 金币，无论是否命中" in text
        assert "正常出价加上超级出价的固定成本（如果使用）" in text
        assert "必须能用当前余额支付" in text and "不能预支命中后获得的金币悬赏" in text
        assert "用来狙击某个正常出价的价位，填的是目标价位，不是支付金额" in text
        assert "超级出价只用于狙击，不能用来赢得拍卖" in text
        assert "提交不符合规则时须重新提交" in text

    def test_settlement_steps_present(self):
        text = render_rules(TEST_GAME, NAMES)
        steps = ["判定命中", "判定撞车", "判定胜负", "扣款与奖励"]
        assert [text.index(step) for step in steps] == sorted(text.index(step) for step in steps)
        assert "至少 2 笔正常出价" in text
        assert "该价位的所有正常出价都算“撞车”，失去获胜资格" in text
        assert "未撞车的正常出价" in text and "最高的一笔获胜" in text
        assert "0 也能获胜" in text and "当天全部拍卖分" in text
        assert "所有正常出价都撞车，则流局" in text
        assert "获胜或撞车的正常出价全额扣除" in text
        assert "其余正常出价全额退还" in text

    def test_scoring_preserves_hit_and_economic_boundaries(self):
        text = render_rules(TEST_GAME, NAMES)
        assert "拍卖奖池中的分数直接计入获胜者的成绩" in text
        assert "份额" not in text and "胜场" not in text
        assert "只要有正常出价就算命中" in text
        assert "判断是否有多人出同一个价格时，只数正常出价；超级出价不计入这个数量" in text
        reward = text.split("每名使用超级出价并命中的玩家，由系统", 1)[1].split("\n", 1)[0]
        assert "3 金币悬赏" in reward and "2 分" in reward
        assert "多人命中同一价位时，每人各领一份奖励" in reward
        assert "同一笔超级出价命中多人时，也只领一份奖励" in reward
        assert "即使该价位有多笔正常出价也算" in text
        assert "自己的正常出价撞车或被狙击" in text
        assert "不影响自己的超级出价命中和领奖" in text
        assert "流局照常发放命中奖励，命中获得的分数当天计入玩家成绩。" in text

    def test_rankings_and_playoffs_only_use_cumulative_scores(self):
        text = render_rules(TEST_GAME, NAMES)
        assert "拍卖分与命中分之和" in text and "总分跨局累计" in text
        assert "排名只看累计总分，同分并列" in text
        assert "第一名同分且还未达到加赛上限" in text and "全体玩家一起加赛" in text
        assert "总分继续累计，每局重新发放金币" in text
        assert "每局加赛后重新排名，直到第一名只剩一人或达到加赛上限" in text
        assert "最多加赛 20 局" in text
        assert "他们都是并列冠军" in text
        assert "其他名次同分不触发加赛" in text
        assert "总胜场" not in text

    def test_publication_includes_yesterday_log_and_all_coins(self):
        text = render_rules(TEST_GAME, NAMES)
        assert "第 2 天起，每天开始时公开" in text
        for fact in ["公开昨日结果", "全员出价明细", "是否使用超级出价及其数值", "谁赢得拍卖，或者是否流局",
                     "各人获得的拍卖分与命中分", "全员当前金币余额，已经包含今日工资"]:
            assert fact in text
        assert "每局结束后公开完整出价日志" in text
        assert "所有玩家同时秘密提交" in text
        assert "每天出价前展示全员累计总分" in text
        assert "最后一局正赛及每局加赛" in text
        assert "本局剩余拍卖分" in text
        assert "每人最多还能获得的命中分：剩余天数 × 2" in text
        assert "今天及本局之后每天的奖池之和。" in text
        assert "剩余天数包含今天" in text
        assert "理论上限，未考虑金币是否够用和命中难度，每人分别计算" in text
        assert "开赛前，每人有一次公开发言" in text
        assert "每局结束后，如果比赛还要继续，每人再发表一次公开发言" in text
        assert "如果比赛已经结束，则发表最终感言" in text
        assert "发言同时提交、同时公开" in text
        assert "发言中的承诺由玩家自行决定是否履行，不会改变游戏规则" in text
        assert "每日秘密出价期间不公开发言" in text
        assert "听取他人发言后，可以私下规划策略" in text
        assert "观众总结只给观众看" in text

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
        assert "各日奖池依次为" in text
        assert "每局奖池合计 6 分" in text
        assert "发放 11 枚金币" in text and "发放 4 枚金币" in text
        assert "0~9 的整数" in text and "固定花费 3 金币" in text
        assert "发放 5 金币悬赏，并获 6 分" in text
        assert "拍卖奖池中的分数直接计入获胜者的成绩" in text
        assert "剩余天数 × 6" in text
        assert "最多加赛 4 局" in text
        assert "他们都是并列冠军" in text
        assert "当天的拍卖分作废，不计入后续奖池。比赛不延长。" in text
        assert "{" not in text and "}" not in text
        system = prompts.build_system_prompt("甲", "人设", "正文", [], names, game)
        assert system.startswith(text)
        assert "其他 2 位玩家是：乙、丙" in system

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
        text = render_rules(TEST_GAME, NAMES, include_note=False)
        system = prompts.build_system_prompt(
            "甲", "人设", "正文", [], NAMES, TEST_GAME
        )
        assert system.startswith(text)

    def test_check_command_works_outside_project_without_writing(self, tmp_path):
        document = tmp_path / "RULES.md"
        document.write_text(render_rules(load_game_settings(), [p["name"] for p in load_players()]), encoding="utf-8")
        before = document.read_bytes()
        # 子进程从外部目录调用真实 CLI main，只把生成物位置隔离到临时目录。
        script = ("from pathlib import Path; from auction_game.game import rules; "
                  f"rules.RULES_PATH = Path({str(document)!r}); raise SystemExit(rules.main())")
        result = subprocess.run(
            [sys.executable, "-B", "-c", script, "--check"],
            cwd=tmp_path, capture_output=True,
            env={**os.environ, "PYTHONPATH": str(ROOT_DIR)},
        )
        assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
        assert document.read_bytes() == before

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
