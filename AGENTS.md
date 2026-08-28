# 项目说明

## 项目目标

用 Python 实现"唯一高价拍卖游戏"：6 名 AI 玩家参加最高唯一出价拍卖，
5 局正赛 + 第一名并列时加赛，含流局顺延、每日分红、发言环节、最终排名。
完整规则见 `prompts.py` 中的 `RULES_TEXT`（与规则原文逐条对齐）。

## 目录与运行

```
/root/auction_game/
├── models.py       # 数据结构：Observation/DayResult/MatchLog/StandingsEntry + 规则常量
├── engine.py       # 单局裁判：出价校验、最高唯一价判定、流局、分红、提前结束
├── tournament.py   # 整场：5 局正赛 + 加赛循环 + SpeechDirector 发言钩子
├── players.py      # Player 抽象 + RandomBot + ScriptedPlayer（测试用）
├── llm_client.py   # 多 provider 分发：modal / hub（protocol=responses|chat）/ anthropic / gemini，
│                   # 后两者当前无玩家使用但代码保留；chat_fn 返回 str
├── ai_player.py    # AIPlayer：只增不改的对话线、出价解析容错、发言/规划/总结
├── controller.py   # 轮级并发执行器 + 单步/连续/暂停状态机 + 按玩家 checkpoint 回滚
├── prompts.py      # 规则全文 + 全部消息模板 + 局日志/排名渲染（markdown 表格）
├── main.py         # 入口：默认 Bot 模式；--ai 为 AI 交互模式（每玩家独立模型）
├── check_llm.py    # 端点连通性冒烟测试（真实联网，--ai 前必跑）
├── analyze.py      # 统计一场 AI 对战的出价/分红/胜场/异常，默认取最新 *_ai 目录，
│                   # 可传 logs/<目录名> 指定；archive_ 开头的归档目录会被忽略
├── config.yaml     # 6 玩家名字+人设+各自 provider/model + 全局 llm.timeout_seconds
├── requirements.txt
├── tests/          # test_engine.py / test_tournament.py / test_ai.py / test_prompts.py /
│                   # test_llm_client.py（119 项全绿，全部零网络）
└── logs/           # 按场次分目录 <时间戳>_<bot|ai>/，latest 软链接指向最近一场（gitignore）
                    # 每场内含 match_<n>.json/.txt，AI 模式另有 ai_<名字>.jsonl、speeches.txt
                    # ai_<名字>.md 与 jsonl 同步双写（markdown 视图，VSCode 打开可实时分玩家阅读）
```

```bash
cd /root/auction_game
python3 -m pytest tests/ -q     # 119 项测试（全部零网络）
python3 main.py --seed 42       # Bot 模式（可复现；冠军随规则变化）
python3 check_llm.py            # 冒烟测试 6 个 LLM 端点（真实联网，--ai 前必跑）
python3 main.py --ai            # AI 模式，需 .env 配齐 MODAL_* / HUB_API_KEY（按 config 实际用到的 provider）
python3 analyze.py              # 统计最新 AI 场次
```

AI 模式交互命令：回车=推进一轮 / c=连续 / s=单步 / p=暂停（保留已完成，恢复后仅补跑未完成）/ q=中止。

## 模型分配

玩家名 = 模型名（Kimi-K3 去掉 moonshotai/ 前缀），人设统一为「做你心中的自己」。
改模型只需改 config.yaml 对应玩家的 provider/model 字段。

| 玩家 | provider | 调用方式 |
|---|---|---|
| Kimi-K3 | modal | OpenAI chat completions（Modal 端点，key=TOKEN_ID.TOKEN_SECRET） |
| deepseek-v4-pro | hub | OpenAI chat completions（hub.oaifree.com/v1，config 中 `protocol: chat`） |
| gemini-3.1-pro-preview | hub | OpenAI responses API（hub 默认 protocol） |
| gpt-5.6-sol | hub | 同上 |
| gemini-3.7-flash | hub | 同上 |
| deepseek-v4-flash | hub | OpenAI chat completions（config 中 `protocol: chat`，同 deepseek-v4-pro） |

注：anthropic / gemini 两个 provider 的代码仍保留在 llm_client.py（当前无玩家使用）。

## 关键设计决策（用户已拍板，勿擅自更改）

1. **玩家类名是 AIPlayer**，与厂商无关，模型/端点走配置；新增端点前先跑 `check_llm.py`。
2. **每局结束公开**：该局完整出价日志 + 当前累计排名 + 每人各局胜场明细。
3. **AI 输出格式**：自由文本推理 + 末尾裸 JSON `{"bid": N}`（提示词示例无围栏——
  实证围栏示例会让模型误以为必须包代码块，裸输出被误判解析失败双次记 0）；
  解析优先取最后一个 ```json 围栏块，无围栏块时兜底取全文最后一个合法裸 JSON
  对象再查 bid 键（无 bid 键即失败，不再向前找）。
4. **对话线只增不改**（保前缀缓存）：system（规则+人设+名单）初始化后永不改，
  所有环节只 append；唯一允许的截断是暂停回滚到本轮 checkpoint。
5. **每天只发一条 user 消息**：「当前是第N局第M天。」非首日随后接「昨日公布」（获胜者带
  「获胜」字样，流局写「流局（无分红）」），余额行放最后且标注「（已含昨日分红）」——
  余额是结算后的值；首日单行不换句。状态行固定带「你的累计总胜场：N（已含罚没）」，
  让模型每天都知道自己的罚没后总胜场（撞车罚没决策依赖它）；状态行末尾另附恒定
  「罚没提示」（罚没只认累计总胜场、正常日仅检查高于获胜价的撞车、作废价位同样只统计
  高于获胜价的撞车）——针对长上下文中模型把罚没口径漂移成「本局胜场」、把作废价位
  误记为「全价位」的实证问题（20260828_200242_ai 场次日志），恒定文案对前缀缓存友好。
6. **不采集推理内容**：`chat_fn` 统一返回 `str`（正文），不需要 `reasoning_content`；
  出口统一过 `strip_think_blocks`，剥离正文内嵌的 `<think>` 推理块（含未闭合截断）。
7. **输出上限内置在 `MODEL_OUTPUT_LIMITS`**（数据来源 https://models.dev/），
  按 API 传参：chat completions 用 max_tokens、responses API 用 max_output_tokens、
  anthropic 必填 max_tokens。
8. **单步粒度 = 一轮并发收集**（线程池），全部任务有定论后才进下一轮；
  暂停 = 等在途调用结束，丢弃暂停后完成的结果并回滚对应玩家，已完成保留，
  恢复后仅补跑未完成项；连续/单步随时切换。
9. **连续两次调用失败（网络/余额等基础设施故障）→ 抛 `CallFailedError` 上报轮级别**：
  一轮收完后打印失败名单，回车仅重跑失败的 AI（q 仍可中止）。
  解析失败（输出格式问题）按规则重试 1 次后记 0，不触发轮级重跑。
10. **发言流程**：开局发言→互看→规划第一局（含第 1 天出价）；每局末同上→规划下局
  （含下一局第 1 天出价）；终局感言→互看→观众总结（不回喂任何对话线）。
  **发言/规划/总结一律不截断**（全文进对话线也全文公开，规划只给自己看不公开）；
  字数只是软要求：开局/局末提示「不超过 300 字」，终局感言与观众总结不限。
  加赛预告只在第 5 局或加赛局结束且第一名仍并列时出现（已达加赛上限则无预告也无规划轮）。
11. **分红除数恒为 8**（玩家永不退场）；一局结束当天不分红；流局日全额返还无分红。
12. **日志按场次分目录**：`logs/<YYYYmmdd_HHMMSS>_<bot|ai>/`，`logs/latest` 相对软链接；
  同秒撞名加 `_2` 后缀；旧日志不做移动/删除，清理由用户决定。
13. **AI 对话日志 md 双写**：`_write_log` 是唯一出口，每条 jsonl 同步渲染追加到同名
  `ai_<名字>.md`；user/assistant 全文收录，调用失败/出价结果以 `> 引用行` 收录；
  jsonl 是结构化源头，md 只是视图可随时重建。
14. **局日志与排名用 markdown 表格渲染**（`render_match_log` / `render_standings`，
  txt 日志与发给 AI 的消息共用同一渲染）。
15. **error 日志条目带轮次上下文**：error 条目写 `context` 字段（「第N局第M天」或环节标签），
  md 引用行渲染为「调用失败（phase，context，第 X 次）」。
16. **高位撞车罚胜场**（防领先者无脑出大数保优势）：每天结算检查撞车价位——正常日仅
  高于获胜价的，流局日全部；同价位玩家总胜场（含本局已获、扣除已罚）不全相同则最多者
  各罚 1 个累计胜场（0 胜天然免疫，并列最多都罚）。罚没只落累计排名，不影响当天胜负、
  金币与本局流程：engine 双账本（`match_wins` 驱动流程、`match_penalties` 落排名），
  局末 `fold_match_penalties` 从最近有净胜场的局折算进 wins_history（各局值不为负）。
  每日公布第 4 项为罚没情况；`RULES_TEXT` 三.6/三.7 与五.1 是规则源头——三.6 按
  「检查范围 / 比较口径 / 罚没」三步算法书写，并显式把比较口径锚定到每日状态行的
  「你的累计总胜场」，五.1 同步强调作废价位只统计高于获胜价的撞车。
17. **规划与下一局第 1 天出价合并**：第 1 天信息恒定（6 金币、0 胜场、8 待拍），
  规划回复末尾必须带裸 JSON `{"bid": N}`，AIPlayer 解析后缓存为 `_pending_day1_bid`，
  引擎第 1 天的 `decide_bid` 直接消费缓存、不再发 LLM 调用（每个局边界省 6 次调用）。
  解析失败与出价同规则（重试 1 次后记 0）；规划开头先清缓存再写，防止暂停重跑残留；
  无缓存时第 1 天回退为正常独立出价调用。`will_continue` 判断含加赛上限余量
  （加赛局 playoff_count 已含本局需 +1，正赛局不需），封顶局末不再空跑规划轮。
18. **打包拍卖**：每局 8 个胜场、每天打包拍 2 个——每天仍只有 1 个最高唯一价，赢家
  一次拿走 2 个胜场，流局则 2 个整体顺延（8÷2 整除无零头）。常量 `WINS_PER_MATCH=8` /
  `WINS_PER_DAY=2` 在 models.py；analyze.py 的「单局多胜」口径相应为 ≥4 胜场（赢超过 1 天）。

## 已踩过的坑（重要）

1. **Kimi K3 强制推理，不能关 thinking**：Modal 端点传 `reasoning_effort="none"` 会 400
   （"Kimi K3 always reasons..."），只能省略该参数或传 low/high/max（当前省略）。
2. **hub 的 claude 走 Anthropic SDK 而非 OpenAI 协议**：`base_url=https://hub.oaifree.com/anthropic`，
   system 提示词必须从 messages[0] 提取到 `system=` 参数，且必须显式传 `max_tokens`。
3. **MiniMax-M3 的 responses 正文内嵌 `<think>` 块**：hub 的 responses API 把推理内容直接拼进
   `output_text`，靠 `strip_think_blocks` 在 chat_fn 出口统一剥离；若输出在 think 中被截断
   （未闭合），剥离后正文为空，会走出价解析失败路径（重试 1 次后记 0）。
