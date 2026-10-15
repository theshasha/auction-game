# 项目说明

用 Python 实现的大模型拍卖个人赛。AI 名单由 `players.yaml` 决定，
Bot 默认 6 人；局数、经济与奖池由 `game.yaml` 决定，第一名并列时加赛。

当前只整理个人赛，不开发新模式；模型服务结构暂不改。

## 代码与文档入口

- `RULES.template.md`：完整规则正文的维护源；`rules.py` 按比赛参数与实际名单渲染，供 `prompts.py` 构建提示词。
- `RULES.md`：生成的可读规则，修改规则时编辑模板并重新生成。
- `game.yaml`、`game_rules.py`：比赛参数及共享校验；生产默认值只维护在 `game.yaml`。
- `models.py`、`engine.py`、`tournament.py`：数据结构、出价结算和比赛流程。
- `ai_player.py`、`llm_client.py`、`prompts.py`：模型玩家、服务调用和提示词。
- `main.py`、`controller.py`：程序入口、运行控制。
- `players.yaml`：玩家名称、人设、模型及服务提供方（provider）。
- `config.yaml`：超时等运行配置；`configuration.py` 供 `main.py`、`check_llm.py` 共享读取配置。
- `analyze.py`：对局统计，统计口径见 `docs/analyze.md`。
- `logs/`：对局日志；`docs/history/`：历史验证记录。

规则文字与代码共同决定实际行为，修改规则时需要一起检查。
每场 AI/Bot 日志目录保存本场 `RULES.md`；统计使用日志内的参数快照。

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

先在 `players.yaml` 中配置玩家名称、人设、模型及 provider，在 `config.yaml` 中设置超时等运行配置，在 `.env` 中填写相应服务的密钥等配置。
AI、Bot 和 benchmark 均读取 `game.yaml`；`check_llm.py` 只检查服务连接，不依赖比赛参数。
以下命令会访问真实服务，可能产生费用，执行前先征求用户同意。

检查模型服务连接：

    python check_llm.py

开始 AI 对局：

    python main.py --ai

运行中：回车推进一次出价或发言，`c` 连续运行，`s` 切回单步，
`p` 暂停，`q` 中止。暂停后恢复会保留已完成结果，只补跑未完成部分。

### 生成规则文档

修改 `game.yaml`、`players.yaml` 或规则模板后刷新可读文档；生成和检查均离线执行：

    python rules.py
    python rules.py --check

`--check` 在文档过期或模板错误时返回非零。比赛提示词直接按本场参数生成，刷新文档不影响比赛运行。

### 查看统计

分析最新 AI 对局：

    python analyze.py

分析指定对局：

    python analyze.py logs/<场次目录>
