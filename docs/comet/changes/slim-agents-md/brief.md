# 目标

精简 AGENTS.md（当前 185 行），删除过于细节或过时的内容，把适合长期保留的知识（14 条设计决策、6 条已踩坑）迁移到 Comet 的分类存储中。

# 范围

- 重写 `/root/auction_game/AGENTS.md`
- 将迁出内容写入 comet memory（project 作用域，按类别逐条）
- 迁移后验证 Comet 侧可检索

# 非目标

- 不修改 RULES.md（规则单一来源不变）
- 不修改任何代码、测试、config.yaml
- 不删除任何日志或归档文件

# 验收示例

- AGENTS.md 明显变短，且不再复述 RULES.md 中已有的规则细节
- 迁出的每条决策/踩坑在 Comet 存储中可检索到（comet memory list / retrieve 验证）
- 精简后的 AGENTS.md 仍保留：项目目标、RULES.md 单一来源指针、运行命令、AI 模式交互命令

# 约束与不变量

- RULES.md 仍是规则全文单一来源
- 不丢失信息：从 AGENTS.md 删掉的内容必须在 Comet 侧有对应条目（或经用户确认属于可丢弃内容）

# 决策

- 工作区：当前目录（用户表示小优化一起提交即可）
- Comet knowledge 是自动从仓库来源生成的记录，无直接撰写入口；可手写的分类存储是 comet memory（--category --scope project）
- Q1（用户确认）：迁出内容写入 comet memory project 作用域，按类别逐条存储（设计决策类、踩坑类），跨会话自动检索注入；AGENTS.md 留索引行
- Q2（用户确认）：适度精简——删 v7 规则要点复述（留 RULES.md 指针）；目录树保留但压缩注释；运行命令保留；14 条决策与 6 条坑全文迁出，AGENTS.md 仅压缩保留每次改代码都要遵守的硬约束（不静默兜底、对话线只增不改、AI 输出格式约定、测试零网络+conftest 夹具+单文件 ≤300 行、改规则先跑 benchmark）并留 memory 索引行
- memory 条目粒度：逐条（14 决策 + 6 坑各一条），类别用「设计决策」「踩坑」

# 待解决问题

（无）

# 验证预期

- `comet memory list` 能看到 20 条 project 作用域条目（14 设计决策 + 6 踩坑）
- `comet memory retrieve` 用相关任务描述能检索到代表性条目
- AGENTS.md 行数从 185 降至约 50 行；不再含规则要点复述；保留目标/指针/命令/硬约束/索引
- `python3 -m pytest tests/ -q` 全绿（文档改动不应影响，作为回归确认）
