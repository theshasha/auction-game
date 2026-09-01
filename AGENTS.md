# 项目说明

项目有 git 环境，状态栏可能有误

## 项目目标

用 Python 实现"唯一高价拍卖游戏"：6 名 AI 玩家参加最高唯一出价拍卖，
5 局正赛 + 第一名并列时加赛，含流局奖池顺延、发言环节、最终排名。

**规则全文单一来源是 `RULES.md`**（prompts.py 加载时读入注入 system 提示词，
改规则只改 RULES.md、重启进程生效）。现行规则（v7）要点：

- 每局固定 6 天，每天拍卖 1 个胜场；流局并入次日奖池，第 6 天未拍出的作废。
- 每局每人初始 7 金币，第 2 天起每天开始 +3 工资；正常出价 0~7 且不超余额。
- 超级出价（收费狙击制）：**不限次数**（每天最多 1 笔），随当天正常出价可
  额外提交一笔 0~7，**每次提交固定消耗 2 金币**；不能等于当天正常出价；
  正常出价 + 2 ≤ 余额才合法。**任何非法提交**（超余额/超范围/非整数/同值/
  总耗资超额/未提交）**整单强制为「正常出价 0，无超级出价」**（v5 同值只丢
  超级，v6 整单作废）。不参与撞车笔数统计、不能获胜。
  命中 = 其数值上 **≥1 笔**正常出价（v5 要求恰好 1 笔，≥2 笔落空；v6 放宽，
  本来就会互撞的热门价位被狙同样命中发赏）→ 该数值**所有**正常出价按撞车
  处理（扣款且不能获胜），狙击者获得系统印发的 3 金币悬赏；多人命中同一
  数值各得 3。0 笔则为落空：白扣 2 金币，无效果。
- 每日三步结算：判定撞车（某数值 ≥2 笔正常出价互撞；外加被超级出价命中的
  数值上所有正常出价视为撞车）→ 未撞车的正常出价中最高一笔获胜 → 金币
  结算（获胜与撞车的正常出价扣款、其余退还、扣除部分系统没收、超级出价
  每次扣 2、命中者各 +3 悬赏）。
- 每天公布（v6 全透明）：第 2 天起每天开始时公布**昨日完整出价明细**
  （全场正常出价数值 + 各人超级出价数值）与**全场每人当前金币余额**
  （含当日工资、含自己）；每局结束公布完整日志与排名。

## 目录与运行

```
/root/auction_game/
├── models.py       # 数据结构：Observation/DayResult/MatchLog/StandingsEntry + 规则常量
│                   # （SUPER_COST/SUPER_BOUNTY/RULES_VERSION 等；MatchLog 序列化带
│                   # rules_version 供 analyze.py 按版本分发口径）
├── RULES.md        # 规则全文（单一来源，prompts.py 加载时读入；v6 起为精简文本，
│                   # 不再含结算示例——见决策 1）；OLD_RULES.md 是 v5 规则存档
├── engine.py       # 单局裁判：整单出价校验（任何非法整单强制为正常出价 0 + 无超级）、
│                   # 三步结算（≥2 笔互撞 + 被狙数值全灭/未撞车正常出价最高者胜/金币）、
│                   # 超级出价结算（每次扣 2、命中 ≥1 笔 +3 悬赏）、奖池顺延
├── tournament.py   # 整场：5 局正赛 + 加赛循环 + SpeechDirector 发言钩子
├── players.py      # Player 抽象（decide_bid 返回 (正常出价, 超级出价) 二元组）
│                   # + RandomBot + ScriptedPlayer（测试用）
├── llm_client.py   # 多 provider 分发：modal / hub（protocol=responses|chat）/ anthropic /
│                   # gemini（modal/gemini 当前无玩家使用但代码保留；anthropic 被珂珂
│                   # 的 kimi-k3 使用——见坑 6）；全部流式调用本地累积正文
├── ai_player.py    # AIPlayer：只增不改的对话线、bid JSON / speech 定界符解析容错、
│                   # 发言/规划/总结
├── controller.py   # 轮级并发执行器 + 单步/连续/暂停状态机 + 按玩家 checkpoint 回滚
├── prompts.py      # 全部消息模板 + 局日志/排名渲染（markdown 表格）；规则文本读 RULES.md
├── main.py         # 入口：默认 Bot 模式；--ai 为 AI 交互模式；--benchmark N 平衡性验证
├── check_llm.py    # 端点连通性冒烟测试（真实联网，--ai 前必跑）
├── analyze.py      # 统计一场对战的出价/奖池/超级出价/胜场/异常，默认取最新 *_ai 目录，
│                   # 可传 logs/<目录名> 指定（Bot 场次亦可）；按日志格式自动适配：
│                   # v6/v7 场次（rules_version="v6"/"v7"）统计超级成本消耗 + 命中次数
│                   # + 悬赏印发量，v5 场次（含 collision_values 字段、无版本标记）不含成本，
│                   # 旧场次保留「凭超级出价获胜/撞车」口径；
│                   # archive_ 开头的归档目录会被忽略
├── config.yaml     # 6 玩家条目：角色名 + persona 人设 + 虚构夺冠率 + provider/model
│                   # + 全局 llm.timeout_seconds
├── requirements.txt
├── tests/          # 全量零网络测试；共享工厂与场景构造器集中在 conftest.py
│                   # （make_day/make_log/make_standings/history/make_obs + 引擎
│                   # run_scenario/bids_by_day/super_by_day + AI 夹具 fake_chat_fn/
│                   # make_player/make_ai_obs），各测试文件 from conftest import。
│                   # 按主题拆小文件（引擎 test_engine{,_super,_observation}、提示词
│                   # test_prompts{,_bid_request,_render,_rules}、AI test_ai{,_speech}、
│                   # 控制器 test_controller），单文件均 ≤300 行——控制 agent 读写与
│                   # 编辑回显的上下文成本；同构用例用 parametrize 合并
└── logs/           # 按场次分目录 <时间戳>_<bot|ai>/，latest 软链接指向最近一场（gitignore）
                    # 每场内含 match_<n>.json/.md，AI 模式另有 ai_<名字>.jsonl、speeches.md
                    # ai_<名字>.md 与 jsonl 同步双写（markdown 视图，VSCode 打开可实时分玩家阅读）
```

玩家名 = 角色名（谐音/联想自模型名，解码见 config.yaml 各条目 name 行尾注释）；
模型、provider、人设、虚构夺冠率全部在 config.yaml 玩家条目内配置，换人换模
直接编辑条目。新增端点前先跑 `check_llm.py`。

```bash
cd /root/auction_game
python3 -m pytest tests/ -q       # 全量测试（全部零网络）
python3 main.py --seed 42         # Bot 模式（可复现）
python3 main.py --benchmark 50    # 平衡性验证：N 场纯随机 + N 场鲸鱼探针（恒出 MAX_BID 对照）
python3 check_llm.py              # 冒烟测试 LLM 端点（真实联网，--ai 前必跑）
python3 main.py --ai              # AI 模式，需 .env 配齐 MODAL_* / HUB_API_KEY（按 config 实际用到的 provider）
python3 analyze.py                # 统计最新 AI 场次
```

AI 模式交互命令：回车=推进一轮 / c=连续 / s=单步 / p=暂停（保留已完成，恢复后仅补跑未完成）/ q=中止。

## 关键设计决策（仍然生效的）

1. **规则文本单一来源**：`RULES_TEXT` 由 prompts.py 加载时读 `RULES.md`
  （有一致性测试）。v5 及以前规则内含结算示例锚定推理，v6 用户决定改用
  精简文本、不再含示例——若 AI 实战出现「撞车后次高唯一价仍获胜」类推理
  错误，第一候选修复是按当时规则改写示例补回（具体示例比重发规则全文
  更有效且零边际成本）。
2. **玩家类名是 AIPlayer**，与厂商无关，模型/端点走配置。
3. **AI 输出格式约定**：出价用裸 JSON `{"bid": N}`（提示词示例不给围栏——
  实证围栏示例会让模型误以为必须包代码块）；公开发言用定界符块
  `【公开发言】…【发言结束】`（实证模型在 JSON 字符串里嵌未转义引号必炸
  `json.loads`，定界符之间是纯文本则不受限）。两条解析路径都带容错与
  旧格式兜底，细节见 ai_player.py。
4. **故障处理哲学：不静默兜底**。网络/空回等调用失败与解析失败、非法出价
  都升级为轮级失败：挂起等人工确认，回车仅重跑失败的玩家，对话线回滚到
  本轮 checkpoint。实证教训：渠道故障产生的垃圾回复末尾恰好带合法 bid JSON
  时，静默记 0 或 fallback 会完全掩盖故障并级联污染其他玩家对话线。
  非法出价先追加写清违规原因的提醒让模型重出价，再犯才升级。
5. **对话线只增不改**（保前缀缓存）：system 初始化后永不改，所有环节只
  append；唯一允许的截断是暂停回滚到本轮 checkpoint。
6. **每天只发一条 user 消息**：当日状态 + 昨日公布 + 「昨日你的结果」行
  （零信息泄露地替模型算清自己的扣款与退款——实证模型自己推导余额会出错
  且不核对，错误级联到整场对手资金推断）；状态行的余额是工资发放后的值
  并直接给出今日可出最高价（实证模型会有「余额 7 打不了 6」的算术错误）。
  超级出价规则**不在每日消息复述**（格式提示只给 JSON 模板 + 提交合法性
  约束；v6 不限次数后状态行的「可用/已用」已随次数状态一并删除）——实证
  20260829_230755 场次：超级规则一天复述 3 遍约 155 字符（占当日消息约
  40%），误解仍集中在不读上下文的弱模型（gemini-3.7-flash 退化到只回裸
  JSON、super_bid 低于正常出价），复述并未起到防误解作用；
  规则强调由 system（RULES.md）与局末重申（决策 11）承担。
  唯一例外是提交合法性约束（同值、正常出价+成本合计余额——v6 违反即
  整单作废）：这不是策略规则，放模板里防止无谓的非法提交。
7. **不采集推理内容 + 全流式调用**：`chat_fn` 统一返回 str 正文，出口统一
  `strip_think_blocks` 剥离内嵌 `<think>` 块；流式化的原因见坑 5。
  输出上限内置在 `MODEL_OUTPUT_LIMITS`（数据来源 https://models.dev/），
  按 API 传参（chat 用 max_tokens、responses 用 max_output_tokens）。
8. **轮级并发执行器**：单步粒度 = 一轮并发收集（线程池），全部任务有定论后
  才进下一轮；暂停 = 等在途调用结束，丢弃暂停后完成的结果并回滚对应玩家。
9. **规划与下一局第 1 天出价合并**：规划回复末尾必须带首日出价 JSON，
  解析后缓存，引擎第 1 天直接消费不再发调用（每个局边界省 6 次调用）。
10. **公开发言先规划后定界符块**：模型先自由规划发言策略（只进自己对话线），
  末尾定界符块给出实际公开发言；提示词带免责句「公开发言不构成对后续出价
  的任何约束」。动机：全文公开发言会让模型把自己说过的话当成接下来要做的
  事（承诺效应），且 fallback 公开回复全文会把故障垃圾喂给全场。
11. **局末发言请求重申精简版结算规则**：实证模型复盘时不套用结算框架
  （把自己撞车推成退款），条款它并非不知道而是复盘时不调用——精简重申
  比依赖记忆可靠。这是对局进行中唯一的规则重申点（每日消息已不复述，
  见决策 6）。
12. **人设只管口吻不管策略**：人设只约束公开发言的口吻，出价推理保持冷静
  认真；人设不含任何策略指令（写死打法会把玩家变成脚本 Bot），也不含
  食物/爱好清单类细节（实证会被模型当成口头禅刷屏，淹没性格表达）。
  system 含【赛前预测】段：虚构夺冠率全员公开（心理战素材）。
13. **日志按场次分目录 + 双写**：jsonl 是结构化源头，md 只是视图可随时重建；
  局日志与排名用 markdown 表格渲染（md 与发给 AI 的消息共用同一渲染）；
  error 条目带轮次上下文。旧日志不做移动/删除，清理由用户决定。
14. **平衡性验证常驻**：`main.py --benchmark N` 跑 N 场纯随机 + N 场鲸鱼探针，
  每次改规则后先跑 benchmark 再进 AI 模式。随机 Bot 每天 30% 概率随机狙击
  （不瞄准、纯噪音，v6 起适配余额约束：正常出价+2 ≤ 余额才用、抽到同值放弃），
  探针结论保守；超级出价相关改动的结论只能靠 AI 实战验证。
  v6 对照（30 场级）：鲸鱼平均总胜场 8.13（v5）→ 7.90（v6），获胜价=6 占比
  41.6% → 38.7%——收费 + 命中放宽略微削弱鲸鱼但量级不变，鲸鱼领先是
  随机 Bot 不瞄准的固有结果，v5 起判定即为「显著领先」。

## 已踩过的坑（重要）

1. **Kimi K3 强制推理，不能关 thinking**：Modal 端点传 `reasoning_effort="none"`
   会 400，只能省略该参数或传 low/high/max（当前省略）。
2. **hub 的 claude 走 Anthropic SDK 而非 OpenAI 协议**：`base_url=.../anthropic`，
   system 提示词必须从 messages[0] 提取到 `system=` 参数，且必须显式传 `max_tokens`。
3. **responses API 可能把推理内容内嵌进正文**：靠 `strip_think_blocks` 在
   chat_fn 出口统一剥离；若输出在 think 中被截断（未闭合），剥离后正文为空，
   抛 `EmptyResponseError` 按调用失败处理（见决策 4）。
4. **gemini-3.7-flash 经 hub responses 在长上下文退化成「续写 transcript」**
   （实证场次 logs/20260829_121925_ai）：回复以编造的残缺 user 回合开头、自带
   本地代码中不存在的 `[Assistant]:` 标记再自问自答——只能来自渠道把 messages
   拍平成文本 transcript 的适配层。诊断方法：jsonl 逐条记录了实际发送的 user
   消息，与回复开头的「回显」逐字比对即可确认本地对话线完好、问题在渠道侧。
   教训即决策 4：解析失败不能静默兜底。
5. **hub 前面的 Cloudflare 掐断非流式慢推理请求（524 / 上游「已取消」）**：
   非流式请求的 Proxy Read Timeout 窗口约 120 秒，慢推理模型思考期间零字节
   输出必撞窗口；OpenAI SDK 默认 max_retries=2 静默重发，游戏侧常常无感成功
   而后台攒下一堆取消记录。修复：全部 provider 改流式，字节持续流动，
   空闲窗口不再触发。
6. **模型自报家门不可靠（蒸馏），不能用来判定渠道错配**（2026-09-01 实测）：
   `provider=hub + model=kimi-k3` 走 responses 协议时，对「介绍你自己」的软
   提问回复自称「Claude」；一度误判为渠道错配。改用正面追问（「你到底是
   Claude 还是 Kimi-K3」）后，responses 与 anthropic 两个协议都答「Kimi，
   月之暗面开发」——两边通的是同一个模型，自报身份只是蒸馏遗留的噪声。
   教训：check_llm 核对模型身份要用尖锐追问（且仍有兜底话术），软提问的
   自称不足为据。珂珂的 kimi-k3 当前挂 anthropic 协议（responses 也验证可用，
   改 provider 一行即可互换）。
