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
        assert "其他玩家的局末公开发言" in text and "【乙】他人发言" in text
        assert "整体替换" in text and "旧记忆" in text and "软目标" in text
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
        assert "并入次日奖池" in text
        assert "每局开始时每人发放 7 枚金币" in text
        assert "第 2 天起每天开始时每人发放 3 枚金币" in text
        assert "金币跨局清零" in text
        assert "0~7 的整数" in text
        assert "第 6 天未拍出的拍卖分作废" in text

    def test_super_bid_rules_present(self):
        text = render_rules(TEST_GAME, NAMES)
        assert "1 笔正常出价" in text and "可再提交 1 笔超级出价" in text
        assert "数值须不同" in text
        assert "每次固定花费 2 金币，无论是否命中" in text
        assert "正常出价加超级出价成本（如有）" in text
        assert "须能用当前余额支付" in text and "不能预支命中悬赏" in text
        assert "只作狙击目标，不算支付金额" in text
        assert "超级出价不参与拍卖获胜判定" in text
        assert "正常出价 0，无超级出价" in text

    def test_settlement_steps_present(self):
        text = render_rules(TEST_GAME, NAMES)
        steps = ["判定命中", "判定撞车", "判定胜负", "扣款与奖励"]
        assert [text.index(step) for step in steps] == sorted(text.index(step) for step in steps)
        assert "至少 2 笔正常出价" in text
        assert "所有正常出价均撞车作废" in text
        assert "未撞车的正常出价" in text and "最高的一笔获胜" in text
        assert "0 也能获胜" in text and "当天全部拍卖分" in text
        assert "所有正常出价均撞车则流局" in text
        assert "获胜或撞车的正常出价全额扣除" in text
        assert "其余正常出价全额退还" in text

    def test_scoring_preserves_hit_and_economic_boundaries(self):
        text = render_rules(TEST_GAME, NAMES)
        assert "拍卖奖池直接计分" in text
        assert "份额" not in text and "胜场" not in text
        assert "先处理非法提交" in text
        assert "只要有正常出价就算命中" in text
        assert "不计入正常出价互撞笔数" in text
        reward = text.split("每名命中者", 1)[1].split("。", 1)[0]
        assert "3 金币悬赏" in reward and "2 分" in reward
        assert "多人命中同一价位各领一份" in reward
        assert "命中多人也只领一份" in reward
        assert "已经有多人撞车也算" in text
        assert "自己的正常出价撞车或被狙击" in text
        assert "不影响自己的命中与领奖" in text
        assert "流局照常发放命中奖励，包括末日流局" in text
        assert "命中分当日入账，不滚存" in text

    def test_rankings_and_playoffs_only_use_cumulative_scores(self):
        text = render_rules(TEST_GAME, NAMES)
        assert "拍卖分与命中分之和" in text and "总分跨局累计" in text
        assert "排名只看累计总分，同分并列" in text
        assert "第一名同分且有加赛额度" in text and "全体玩家加赛" in text
        assert "总分继续累计，金币照常重置" in text
        assert "每局后重新排名，第一名唯一即结束" in text
        assert "最多加赛 20 局" in text
        assert "保留并列冠军" in text
        assert "其他名次同分不触发加赛" in text
        assert "总胜场" not in text

    def test_publication_includes_yesterday_log_and_all_coins(self):
        text = render_rules(TEST_GAME, NAMES)
        assert "第 2 天起，每天开始时公开" in text
        for fact in ["昨日全员出价明细", "是否使用超级出价及其数值", "获胜者或流局",
                     "各人拍卖分与命中分", "全员当前金币余额（含今日工资）"]:
            assert fact in text
        assert "每局结束后公开完整出价日志" in text
        assert "所有玩家同时秘密提交" in text
        assert "每天出价前展示全员累计总分" in text
        assert "最后一局正赛及每局加赛" in text
        assert "本局剩余拍卖分" in text
        assert "每人理论命中分上限“剩余天数 × 2”" in text
        assert "余量均含今天" in text and "拍卖分包含滚存" in text
        assert "忽略资金与命中难度，各人独立计算" in text
        assert "开赛前和每个非终局的局末" in text
        assert "终局后发表最终感言" in text
        assert "发言同时提交、同时公开" in text
        assert "无规则约束力" in text
        assert "每日秘密出价期间不公开发言" in text
        assert "听取他人发言后可私下规划策略" in text
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
        assert "每局计划共 6 分拍卖奖池" in text
        assert "发放 11 枚金币" in text and "发放 4 枚金币" in text
        assert "0~9 的整数" in text and "固定花费 3 金币" in text
        assert "发放 5 金币悬赏，并获 6 分" in text
        assert "拍卖奖池直接计分" in text
        assert "剩余天数 × 6" in text
        assert "最多加赛 4 局" in text
        assert "保留并列冠军" in text
        assert "第 3 天未拍出的拍卖分作废" in text
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
