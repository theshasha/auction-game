# 项目说明

项目有 git 环境，状态栏可能有误。

## 项目目标

用 Python 实现「唯一高价拍卖游戏」：6 名 AI 玩家参加最高唯一出价拍卖，
5 局正赛 + 第一名并列时加赛，含流局奖池顺延、发言环节、最终排名。

**规则全文单一来源是 `RULES.md`**（prompts.py 加载时读入注入 system 提示词，
改规则只改 RULES.md、重启进程生效；OLD_RULES.md 是 v5 存档）。现行版本 v7，
规则细节一律以 RULES.md 为准，本文不复述。

## 目录与运行

```
/root/auction_game/
├── models.py       # 数据结构与规则常量（SUPER_COST/SUPER_BOUNTY/RULES_VERSION 等）
├── RULES.md        # 规则全文（单一来源，prompts.py 加载时读入）
├── engine.py       # 单局裁判：整单出价校验 + 三步结算 + 超级出价结算 + 奖池顺延
├── tournament.py   # 整场：5 局正赛 + 加赛循环 + 发言钩子
├── players.py      # Player 抽象 + RandomBot + ScriptedPlayer（测试用）
├── llm_client.py   # 多 provider 分发（modal/hub/anthropic/gemini），全部流式
├── ai_player.py    # AIPlayer：只增不改对话线、bid JSON / 发言定界符解析容错
├── controller.py   # 轮级并发执行器 + 单步/连续/暂停状态机 + checkpoint 回滚
├── prompts.py      # 全部消息模板 + 局日志/排名渲染；规则文本读 RULES.md
├── main.py         # 入口：默认 Bot 模式；--ai 交互模式；--benchmark N 平衡性验证
├── check_llm.py    # 端点连通性冒烟测试（真实联网，--ai 前必跑）
├── analyze.py      # 统计一场对战（默认取最新 *_ai 目录），按日志版本自动适配
├── config.yaml     # 6 玩家条目（角色名+人设+夺冠率+provider/model）与全局超时
├── tests/          # 全量零网络测试；共享工厂与夹具集中在 conftest.py
└── logs/           # 按场次分目录 <时间戳>_<bot|ai>/，latest 软链接指向最近一场
```

玩家名 = 角色名；模型、provider、人设、夺冠率全部在 config.yaml 玩家条目内配置，
换人换模直接编辑条目。

```bash
cd /root/auction_game
python3 -m pytest tests/ -q       # 全量测试（全部零网络）
python3 main.py --seed 42         # Bot 模式（可复现）
python3 main.py --benchmark 50    # 平衡性验证：N 场纯随机 + N 场鲸鱼探针
python3 check_llm.py              # 冒烟测试 LLM 端点（真实联网，--ai 前必跑）
python3 main.py --ai              # AI 模式，需 .env 配齐 MODAL_* / HUB_API_KEY
python3 analyze.py                # 统计最新 AI 场次
```

AI 模式交互命令：回车=推进一轮 / c=连续 / s=单步 / p=暂停（保留已完成，恢复后仅补跑未完成）/ q=中止。

## 硬约束（每次改代码都要遵守）

- **不静默兜底**：网络/空回等调用失败、解析失败、非法出价一律升级为轮级失败，
  挂起等人工确认；非法出价先追加写清违规原因的提醒让模型重出价，再犯才升级。
- **对话线只增不改**（保前缀缓存）：system 初始化后永不改，所有环节只 append，
  唯一允许的截断是暂停回滚到本轮 checkpoint；每天只发一条 user 消息。
- **AI 输出格式**：出价用裸 JSON `{"bid": N}`（提示词示例不给围栏）；
  公开发言用定界符块 `【公开发言】…【发言结束】`。
- **测试**：全量零网络；共享工厂/夹具集中在 tests/conftest.py；单测试文件 ≤300 行。
- **规则改动**：只改 RULES.md；改完先跑 `main.py --benchmark N` 再进 AI 模式；
  新增端点前先跑 check_llm.py。

## 设计决策与踩坑

14 项设计决策与 6 项踩坑的完整原文（含实证教训）已迁入 comet memory
（project 作用域，类别「设计决策」「踩坑」），跨会话按任务自动检索注入；
手动查看：`comet memory list --category 设计决策` / `--category 踩坑`。
