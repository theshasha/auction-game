# 项目说明

用 Python 实现的大模型拍卖个人赛。AI 名单由 `players.yaml` 决定，
Bot 默认 6 人；局数、经济与奖池由 `game.yaml` 决定，第一名并列时加赛。

当前优先选型、接入成熟通用模型接口，具体方案先调查并经用户确认后实施；
接入完成后再讨论指挥官设计，确认后开发。

## 代码与文档入口

- `RULES.template.md`：完整规则正文的维护源；`rules.py` 按比赛参数与实际名单渲染，供 `prompts.py` 构建提示词。
- `RULES.md`：生成的可读规则，修改规则时编辑模板并重新生成。
- `game.yaml`、`game_rules.py`：比赛参数及共享校验；生产默认值只维护在 `game.yaml`。
- `models.py`、`engine.py`、`tournament.py`：数据结构、出价结算和比赛流程。
- `ai_player.py`、`llm_client.py`、`prompts.py`：模型玩家、Pydantic AI 模型工厂与过渡文本接口、提示词。
- `main.py`：命令行解析与模式分派；`runners.py`：Bot/AI 玩家组装与比赛启动。
- `ai_speech.py`：AI 发言编排；`run_output.py`：运行目录、局末日志与最终结果展示。
- `benchmark.py`：批量策略对照及统计；`players.py`：Bot 名单和随机/脚本玩家。
- `controller.py`：单步、连续、暂停与轮级并行控制。
- `players.yaml`：玩家名称、人设、渠道引用（provider）、显式协议与输出预算。
- `config.yaml`：超时及渠道地址、密钥环境变量名；`configuration.py` 集中校验运行与玩家配置。
- `analyze.py`：对局统计，统计口径见 `docs/analyze.md`。
- `logs/`：对局日志；`docs/history/`：历史验证记录。

规则文字与代码共同决定实际行为，修改规则时需要一起检查。
每场 AI/Bot 日志目录保存本场 `RULES.md`；统计使用日志内的参数快照。

## 运行

在项目根目录使用现有 `C:\Python313\python.exe` 安装依赖，再执行以下命令：

    python -m pip install -r requirements.txt

### 测试与随机玩家

自动化测试不调用真实模型服务：

    python -m pytest tests/ -q

运行一场随机玩家对局，固定随机种子便于复现：

    python main.py --seed 42

批量运行纯随机与最高可付价策略对照，场数须为正整数；两组各 N 场，共 2N 场，不写日志。
策略玩家每天出 `min(出价上限, 当前余额)`，不使用超级出价；策略与随机对手的均值比较均取第二组。

    python main.py --benchmark 50

`main.py` 成功或用户主动 `q` 中止返回 0，已处理的启动配置错误返回 1，命令行参数错误返回 2。

### AI 对局

先在 `players.yaml` 中配置玩家名称、人设、原始模型名、provider、protocol、max_tokens。
provider 引用 `config.yaml` 的 `llm.channels`；渠道定义 base_url 和 api_key_env，密钥写入 `.env` 对应变量。
protocol 必填 `chat` / `responses` / `anthropic`，渠道名不决定协议。OpenAI 地址保留 `/v1`，Anthropic 地址由 SDK 追加 `/v1/messages`。
AI、Bot 和 benchmark 均读取 `game.yaml`；`check_llm.py` 只检查服务连接，不依赖比赛参数。
当前 AIPlayer 仍使用薄文本接口；SDK 与模型层不重试，失败重试仍归 AIPlayer。继续模型迁移或排查协议兼容时先读 `docs/pydantic-ai-integration.md`。
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
