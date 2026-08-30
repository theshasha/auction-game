"""规则文本与局末规则重申的测试：RULES_TEXT 单一来源（RULES.md）、
RULES_REMINDER 的覆盖要点与出现位置。共享工厂见 conftest.py。"""

import prompts
from conftest import NAMES, make_log, make_standings


class TestRulesReminder:
    """局末发言请求在展示局日志前重申结算三步（精简版），其余请求不带。"""

    def test_match_end_speech_contains_reminder_before_log(self):
        text = prompts.match_end_speech_request(
            make_log(), make_standings({n: [0] for n in NAMES}), True
        )
        assert "【规则要点重申】" in text
        assert text.index("【规则要点重申】") < text.index("本局完整出价日志")

    def test_reminder_covers_three_steps_and_super_bid(self):
        reminder = prompts.RULES_REMINDER
        assert "两笔及以上即撞车" in reminder
        assert "未撞车的正常出价中最高的一笔获胜" in reminder
        assert "全额退还" in reminder
        # 超级出价要点（v5 狙击悬赏制）：每局 1 次命中即恢复、只狙击不能获胜、
        # 不扣金币、命中 +3 悬赏、使用情况全透明公布（谁用了、数值几、是否命中）
        assert "超级出价每人每局 1 次（命中即恢复，无上限）" in reminder
        assert "不能获胜" in reminder
        assert "3 金币悬赏" in reminder
        assert "谁用了、数值几、是否命中" in reminder

    def test_other_requests_have_no_reminder(self):
        standings = make_standings({n: [0] for n in NAMES})
        assert "【规则要点重申】" not in prompts.next_match_plan_request({"乙": "发言"})
        assert "【规则要点重申】" not in prompts.final_speech_request(standings, make_log(match_index=5))


class TestRulesText:
    """规则全文单一来源是 RULES.md：RULES_TEXT 即文件内容，要点与文件逐条对齐。"""

    def test_rules_text_loaded_from_rules_md(self):
        from pathlib import Path
        rules_md = (Path(prompts.__file__).parent / "RULES.md").read_text(encoding="utf-8").strip()
        assert prompts.RULES_TEXT == rules_md

    def test_core_mechanics_present(self):
        text = prompts.RULES_TEXT
        assert "每局固定 6 天" in text
        assert "每天拍卖 1 个胜场，每局共 6 个胜场" in text
        assert "奖池并入次日" in text
        assert "每天开始时每人发放 3 枚金币（工资）" in text
        assert "出价为 0 到 6 的整数" in text

    def test_super_bid_rules_present(self):
        text = prompts.RULES_TEXT
        assert "每名玩家每局拥有 1 次超级出价机会" in text
        assert "不受余额限制" in text
        # 规则 v5：超级出价是狙击工具——不参与撞车笔数、不能获胜、不扣金币，
        # 命中唯一正常出价使其按撞车处理，狙击者得 3 金币悬赏且机会恢复
        assert "不能等于你当天的正常出价" in text
        assert "不参与撞车的笔数统计" in text
        assert "不能获胜、任何结局都不扣金币" in text
        assert "恰好只有 1 笔正常出价" in text
        assert "3 金币悬赏" in text
        assert "机会立即恢复" in text

    def test_settlement_three_steps_present(self):
        text = prompts.RULES_TEXT
        assert "判定撞车" in text
        assert "判定胜负" in text
        assert "金币结算" in text
        assert "其余正常出价全额退还" in text
        assert "流局日同样扣除" in text
        assert "系统没收" in text

    def test_no_cap_tax_anymore(self):
        text = prompts.RULES_TEXT
        assert "冲顶税" not in text
        assert "转判" not in text

    def test_settlement_examples_present(self):
        text = prompts.RULES_TEXT
        # 示例一：超级出价命中、他人捡漏；示例二：最高出价撞车、次高唯一价获胜
        assert "结算示例一" in text and "结算示例二" in text
        assert "未撞车的正常出价是 5、4、2，其中最高的 5 获胜" in text
        assert "唯一出价中最高的是 2" in text

    def test_publication_includes_super_usage_with_value_and_hit(self):
        # 规则五.1（v5 全透明）：撞车价位报具体数值，超级出价报谁用了/数值/是否命中
        text = prompts.RULES_TEXT
        assert "获胜者身份与获胜价" in text
        assert "撞车价位的具体" in text
        assert "谁使用了" in text
        assert "数值是几、是否命中" in text

    def test_publication_includes_others_info_from_day2(self):
        # 规则五.1：第 2 天起每天开始时公布其他人总金币与剩余超级出价总数
        text = prompts.RULES_TEXT
        assert "其他玩家的总金币余额" in text
        assert "剩余超级出价总数" in text
