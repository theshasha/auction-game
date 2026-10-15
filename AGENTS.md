# 项目说明

用 Python 实现的大模型拍卖游戏，目前为 6 名玩家参加个人赛，
进行 5 局正赛，第一名并列时加赛。

后续优先开发指挥官与团队模式，具体方案待确认。

## 代码与文档入口

- `RULES.md`：比赛规则，`prompts.py` 会读取并放入模型提示词。
- `models.py`、`engine.py`、`tournament.py`：数据结构、出价结算和比赛流程。
- `ai_player.py`、`llm_client.py`、`prompts.py`：模型玩家、服务调用和提示词。
- `main.py`、`controller.py`：程序入口、运行控制。
- `config.yaml`：玩家名称、人设、模型、服务提供方及超时配置。
- `analyze.py`：对局统计，统计口径见 `docs/analyze.md`。
- `logs/`：对局日志；`docs/history/`：历史验证记录。

规则文字与代码共同决定实际行为，修改规则时需要一起检查。

## 运行

在项目根目录执行：

### 测试与随机玩家

自动化测试不调用真实模型服务：

    python -m pytest tests/ -q

运行一场随机玩家对局，固定随机种子便于复现：

    python main.py --seed 42

批量运行随机玩家和固定高价策略的对照实验：

    python main.py --benchmark 50

### AI 对局

先在 `config.yaml` 中配置玩家及模型，在 `.env` 中填写相应服务的密钥等配置。
以下命令会访问真实服务，可能产生费用，执行前先征求用户同意。

检查模型服务连接：

    python check_llm.py

开始 AI 对局：

    python main.py --ai

运行中：回车推进一次出价或发言，`c` 连续运行，`s` 切回单步，
`p` 暂停，`q` 中止。暂停后恢复会保留已完成结果，只补跑未完成部分。

### 查看统计

分析最新 AI 对局：

    python analyze.py

分析指定对局：

    python analyze.py logs/<场次目录>
