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
        assert "两笔及以上正常出价即撞车" in reminder
        assert "未撞车的正常出价中最高的一笔获胜" in reminder
        assert "全额退还" in reminder
        # 超级出价要点（v6 收费狙击制）：不限次数、每次固定扣 2 金币、只狙击不能获胜、
        # 命中（≥1 笔正常出价）+3 悬赏、出价明细与全场余额每日公布
        assert "超级出价不限次数（每天最多 1 笔）" in reminder
        assert "固定扣除 2 金币" in reminder
        assert "不能获胜" in reminder
        assert "3 金币悬赏" in reminder
        assert "至少有 1 笔正常出价" in reminder
        assert "昨日完整出价明细" in reminder
        assert "全场每人的当前金币余额" in reminder

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
        assert "每局 6 天" in text
        assert "每天拍卖 1 个胜场，每局共 6 个胜场" in text
        assert "并入次日奖池" in text
        assert "每天开始时每人发放 3 枚金币" in text
        assert "0~7的整数" in text

    def test_super_bid_rules_present(self):
        text = prompts.RULES_TEXT
        # 规则 v6：超级出价是收费狙击工具——不限次数（每天最多 1 笔）、
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
        text = prompts.RULES_TEXT
        assert "判定撞车" in text
        assert "判定胜负" in text
        assert "金币结算" in text
        assert "未获胜且未撞车的正常出价全额退还" in text
        # 流局判定：当天正常出价全部撞车则无人获胜
        assert "若当天所有正常出价均撞车" in text
        assert "宣告流局" in text

    def test_no_cap_tax_anymore(self):
        text = prompts.RULES_TEXT
        assert "冲顶税" not in text
        assert "转判" not in text

    def test_publication_includes_yesterday_log_and_all_coins(self):
        # 规则四.1（v6）：第 2 天起公布昨日完整出价明细与全场每人当前余额
        text = prompts.RULES_TEXT
        assert "昨日出价日志" in text
        assert "具体出价明细" in text
        assert "全场每位玩家当前的总金币数" in text
        assert "每局结束后，公开该局完整出价日志" in text
