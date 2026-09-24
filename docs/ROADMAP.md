# demo-agent 能力补全 Roadmap

本项目的定位是**不依赖 Agent 框架**，用 OpenAI Python SDK 手写 agent loop。这份 roadmap 回答一个问题：**现有代码还需要补什么，能力面才能对齐 LangChain / LangGraph（v1 `create_agent`）这类框架**。排序原则是"对能力的影响 × 面试/教学价值 ÷ 工作量"，前四项合计约 3–4 个工作日。

每一项都标注了框架里的对应物，便于对照学习：手写实现的价值正在于能看清框架替你做了什么。

## 状态总览

| # | 能力 | 框架对应物 | 优先级 | 状态 |
|---|------|-----------|--------|------|
| 1 | 上下文管理（截断/摘要） | `trim_messages`、summarization | P0 | 进行中（纯截断已上线） |
| 2 | 可观测性与 token 计量 | LangSmith / `astream_events` 元数据 | P0 | 未开始 |
| 3 | 结构化输出 | `with_structured_output` | P1 | 未开始 |
| 4 | 重试与降级 | provider fallback、`max_retries` | P1 | 未开始 |
| 5 | 并行工具执行 | `ToolNode` 并发执行 | P2 | 未开始 |
| 6 | 审批 / Human-in-the-loop | LangGraph `interrupt` | P2 | 未开始 |
| — | MCP 接入、多 agent、checkpointer、缓存 | — | 观望 | 不做 |

**已对齐框架的部分**（不需要补）：流式 tool_calls 增量按 index 聚合、同轮并行调用的顺序执行与 `tool_call_id` 回填、`max_turns` ≈ `recursion_limit`、工具错误/编排错误两条失败路径分离、模块化工具注册 ≈ `@tool`、结构化事件流 ≈ `astream_events`。

---

## P0-1 上下文管理：截断与摘要

**问题**：`api.py` 每轮从 SQLite 父消息链重建完整 `items`，没有任何长度控制。会话一长直接撞 context window 上限，整轮 `error`。这是当前功能上唯一的硬缺口。

**目标**：构造 `items` 后增加一道上下文预算控制，两级策略：

1. **截断（trim）**：估算消息链 token 数（无 tokenizer 依赖，`len(text)` 一字一 token 的保守估算：对中文偏大、对英文更偏大，方向安全——宁可提前截断，不可超限报错），超过预算（24K 起步，glm-4.6 窗口 128K，留 5 倍余量）时保留 system 消息 + 最近 N 轮；
2. **摘要（summarize）**：被截掉的历史用一次独立模型调用压缩成一条摘要消息（role 用 `system` 或 `user` 前缀标注"历史摘要"），拼在 system 之后。

**最终设计**（2026-09-23 与 Madam 讨论定稿）：单一滚动摘要 + 会话级两列 + 增量合并 + 失败降级截断。

- **表结构**：`chat_sessions` 新增 `summary TEXT`（当前摘要，从未触发时为 NULL）与 `summary_upto_message_id INTEGER`（摘要游标，已收编的最大 message_id）。摘要不进 `chat_messages` 消息链——链是严格父指针链（user/assistant 交替 + stale_parent 校验），中间插入会破坏约束；摘要挂在会话上，粒度才对（它覆盖一批消息，不属于任何一轮对话）。原文全量保留为 source of truth，摘要只是可随时重建的派生数据（清空两列即可全量重摘）。
- **为什么不存多份摘要**：上下文中只保留一份生效摘要。多个版本并排会随会话无限增殖、把预算吃掉，且边界信息重复矛盾。每次压缩是"旧摘要 + 新收编轮次 → 合并成新摘要，直接覆盖"，即增量合并的滚动摘要。历史版本表（`session_summaries`）暂不做，原文兜底，需要调摘要质量对比时再加。
- **每轮流程**（`api.py` 构造 `items` 时）：
  1. 用游标切分消息链：`id > summary_upto` 的为活着的原话，其余由摘要代表；
  2. `items` = system + 摘要消息（若有，role=`user`，前缀"以下是早期对话摘要"）+ 活着的原话 + 本轮新消息。`items` 只发给模型，不展示给用户；用户看的是 SQLite 原文（存储 ≠ 上下文）。
- **触发与滚动**（`done` 之后做，避免阻塞用户本轮回复）：
  1. 活着的原话超过触发线（约 12K token）时，从最新往回保留约 8K 原话（`pick_recent_within`），其余为本次收编段 `to_absorb`；
  2. 调一次独立模型调用 `summarize(old_summary, to_absorb, max_tokens≈2000)`，prompt 保留用户目标/决定/数字/未完成事项，丢弃寒暄与过程；
  3. 模型返回即为完整新摘要，`UPDATE chat_sessions SET summary=?, summary_upto=to_absorb 末条 id`，单次事务覆盖。
- **失败处理**：摘要调用失败 → 本轮跳过（游标不动，状态自洽），下轮再试；持续失败 → `build_context` 侧退化为纯截断（超出预算的旧原话直接不进 items，不报错）。截断牺牲品的准确定义：**living（id > 游标）中 id 最小的若干条；游标之前的部分由摘要代表，不参与截断**。三个区域：游标之前（摘要代言，常驻 items）、living 被截断部分（无人代言，本轮模型不可见，但库里完整）、living 保留区（原话进 items）。已知缺陷接受：早期内容被反复蒸馏，细节必然衰减，摘要 prompt 强调保留事实性信息缓解。
- **积压与补吃**：摘要持续失败期间，未收编的 living 单调增长，且截断丢弃线持续右移（中间段在模型视野持续不可见）。若积压过大，恢复后一次 `summarize` 输入过大易再次失败（恶性循环）。对策：**单次收编上限（约 8K token）**，一次吃不完则游标部分推进、后续每轮继续吃，把一次性大手术拆成多次小手术。补吃无需特殊逻辑：游标在库里，故障恢复/进程重启后下一轮 `maybe_roll` 自然触发。
- **暂不做**：摘要版本历史表、检索式记忆（RAG memory）、tokenizer 精确计数（中文场景 `len(text)` 粗估即可）。

**改动位置**：新增 `server/agent/context_budget.py`（`build_context` / `maybe_roll` / `pick_recent_within` / `summarize` 纯函数 + prompt），`database/` 加两列与更新方法，`api.py` 接线。

**测试**：游标切分（NULL 游标/正常游标）、预算触发与不触发、收编段与保留段的划分边界、摘要拼接顺序、摘要失败降级为截断、单条消息超预算的极端情况。

**注意**：这与 lost-in-the-middle 现象直接相关——控制进上下文的信息量本身就是能力，不是省钱的权宜之计。近期对话保持原话放尾部（模型注意力最好的位置），摘要放前部。

**实现顺序**：先做纯截断（游标未引入前的 trim 兜底逻辑）→ 埋点/表结构 → 滚动摘要 → stats 查询接口。

## P0-2 可观测性与 token 计量

**问题**：现在只有文本日志，无法回答"这轮跑了几次模型、每个工具耗时多少、花了多少 token"。LangChain 生态里这是 LangSmith 一键接入的能力。

**目标**：

1. `stream_events` 内部记录结构化数据（不改变事件协议）：每轮的模型调用耗时、工具名 + 耗时、是否出错；
2. token 计量：流式响应的 `usage` 在最后一个 chunk（需确认智谱兼容接口是否返回，不返回则退化为按文本长度估算），按会话累计；
3. 落一张 SQLite 表（`agent_turns` / `tool_runs`）与 `chat_messages.meta` 列，并加一个 `GET /api/sessions/{id}/stats` 只读接口（本期做）。

**埋点设计定稿**（2026-09-23 与 Madam 讨论定稿）：

- **数据流**：loop 把用量/耗时交给 `context.recorder`（只管记，不管存），SSE 结束时 `api.py` 统一落库——loop 全程不碰数据库；测试时给 recorder 换内存假账本即可。
- **双存储**：`agent_turns` 是逐次模型请求的账本（一行 = 一次请求，外键 `message_id` 锚**用户消息**——轮开始前已存在、中途失败不丢账）；`chat_messages.meta`（JSON 列，加在现有表上）是挂在**助手消息**身上的汇总挂牌（账是"生成这条回答"的成本，且写入时数字在手边，不回改旧消息）。不存在"指向助手消息"的外键。
- **关联层级**：session → 用户消息 → `agent_turns`（一次请求一行）→ `tool_runs`（一次工具执行一行，外键 `agent_turn_id`，一对多；一次模型请求带多个工具调用是项目已支持的常态）。
- **工具计时与成败**：loop 在 `execute_tool` 前后用已有的 `time.monotonic()` 打点交 recorder，工具本身零改动。成败判定：`execute_tool` 改为显式返回 `(output, ok)` 二元组（catch 到异常即 `ok=False`），不靠返回字符串前缀猜；改动点在 `tools/__init__.py` 签名。
- **工具输入输出**：`tool_runs` 存截断副本 `args_excerpt` / `result_excerpt`（各约 500 字）。业界惯例（LangSmith / LangFuse / ChatGPT 工具回放）均存输入输出但都截断——账本要的是"足以判断这次执行正常吗"，全量留给原始来源，泄漏面也小。
- **stats 接口**：查询时对 `agent_turns` 现场聚合（`SUM(prompt_tokens)` 等），不存累计总数——能推导的数据不冗余存，账本是唯一事实来源。响应含 `turns` / `total_prompt_tokens` / `total_completion_tokens` / `estimated`。

**改动位置**：`agent/loop.py`（埋点）、`database/`（新表 `agent_turns`/`tool_runs` + `chat_messages.meta` 列）、`api.py`（stats 查询接口）、`tools/__init__.py`（`execute_tool` 返回 `(output, ok)`）。

**前端展示定稿**（2026-09-23 与 Madam 讨论定稿，P0 内实现）：

1. **会话 stats 轻量入口**：会话标题旁小图标/文字，点开显示"本会话：N 轮请求 / X token"（数据来自 stats 接口）。
2. **压缩分隔线（静态档）**：历史接口返回 `summary` + `summary_upto_message_id`，前端在「id ≤ 游标的最后一条消息」与下一条消息之间画分隔线，显示"更早的对话已压缩为摘要"。展开被收编原文留作后续小迭代（存储全量已支持，纯前端状态开关）。参考形态：Claude Code 的 auto-compact 标记（agent 工具类产品的可见做法；ChatGPT/Claude 消费版完全隐藏）。
3. **工具过程可见**（默认收起、点击展开，同 ChatGPT 工具卡片交互）：
   - 实时：`tool_result` SSE 事件加 `elapsed` 字段（协议变更，按 AGENTS.md 同步检查五处：loop / api / adapter / tests / README）；工具收起为一行卡片（如 `🔧 web_search · 3.2s`），点击展开参数与返回。
   - 历史：`GET /api/sessions/{id}` 返回附带该会话的 tool_runs（按 agent_turn → 用户消息关联渲染到对应回复底下）。注意历史视图展开的是 500 字截断副本（result_excerpt），非全量——存储决策的自然结果，UI 标注"已截断"；实时事件带的是完整返回。

**测试**：mock 流上验证计量聚合正确；真实接口 smoke test 验证 usage 字段是否可用（仅无副作用请求）。

**工作量**：约 1 天（含 usage 字段可用性验证）。

## P1-3 结构化输出

**问题**：所有输出都是自由文本。框架的 `with_structured_output(schema)` 能强约束 JSON 输出，教学上对应"让 agent 输出任务清单/评分卡"这类真实需求。

**目标**：在 `agent/` 增加一个入口：单工具 + `tool_choice` 强制调用 + 解析后摘出结果。实现路径：

1. 定义一个 `submit_result` 工具（参数即目标 schema），请求时 `tool_choice` 指定强制调用；
2. 流结束后从 `items` 最后的 tool call 里解析 JSON 返回；
3. 复用现有 loop，`stream_events` 不动，新增一个薄封装函数。

**测试**：mock 流验证强制调用解析、非法 JSON 错误路径。

**工作量**：约半天。

## P1-4 重试与降级

**问题**：`client.py` 设置 `max_retries=0`（当时的取舍是避免卡住的请求被拉长数倍），网络抖动一次即整轮 `error`，没有 fallback。

**目标**：

1. 对网络类异常（连接错误、超时、5xx）做 1–2 次指数退避重试；4xx 不重试；
2. 可选：`.env` 配置备用 `BASE_URL`/`MODEL`，主模型连续失败后降级一次。降级目标必须同样是 OpenAI 兼容接口。

**改动位置**：`agent/client.py` 或 `agent/loop.py` 的请求包装处。

**测试**：mock 客户端验证重试触发、退避、最终失败转 `error` 事件；验证 4xx 不重试。

**工作量**：约半天。

## P2-5 并行工具执行

**问题**：`_run_tool_calls` 按模型给出的顺序**串行**执行同轮多个 tool call。当前 7 个工具里只有 `web_search`/`fetch_url` 是耗时网络调用，批量搜索时能感知差异。

**目标**：区分副作用类别——`read_file`/`web_search`/`fetch_url`/`get_weather`/`parse_attached_document` 为只读工具，可并发执行；`write_file`、`calculate`（eval，有副作用风险）保持串行且顺序不变。结果按 `tool_call_id` 对应回填，事件仍按确定顺序产出。

**注意**：这会改变"副作用的实际发生顺序等于模型调用顺序"这一现有不变量（AGENTS.md 有记录），需要同步更新文档与测试。并发后事件产出顺序需要重新定义（如：按调用顺序发 `tool_call`，全部完成后按同序发 `tool_result`）。

**工作量**：约半天，主要花在事件顺序语义和测试上。

## P2-6 审批 / Human-in-the-loop

**问题**：`write_file`、`calculate`（eval）这类副作用工具，模型每次调用都直接执行，没有任何确认机制。AGENTS.md 已把这条列为高风险边界。LangGraph 的 `interrupt` 原语是框架级解法。

**目标**：副作用工具执行前暂停，等用户确认后恢复。这是本 roadmap 中唯一需要**动 SSE 事件协议和前端**的项目：

1. 事件协议新增 `approval_required`（携带 tool call id、name、args）；
2. 后端把挂起的 agent 状态（`items` 列表）序列化暂存（内存 dict 或落库）；
3. 新增 `POST /api/chat/{session_id}/approve` 恢复执行；拒绝则把"用户拒绝"作为 tool 结果回填，让模型自行调整；
4. 前端在 `adapter/` 类型与 UI 上补确认交互；
5. 超时未审批自动拒绝。

**建议**：单独作为一个里程碑，先出协议设计稿评审再动手。它也是把 `calculate` 的 eval 风险降下来的前置（审批 + 受限表达式解析双管齐下）。

**工作量**：2–3 天（含前端）。

---

## 明确不做的（观望项）

- **MCP 接入**（工具从外部 MCP server 动态加载）：与"手写理解工具协议"的教学目标方向相反，等工具数量翻倍以上再考虑。
- **多 agent / 子图**：刻意单 agent，教学定位合理。
- **checkpointer / time-travel**：现有"只存最终消息 + 每轮重建"的语义更简单且够用；引入会与 SQLite 持久化打架。
- **prompt 缓存、batch、多模态输入**：当前场景用不上。

## 验收与验证约定

- 每项完成后运行 `server/` 全量后端 unittest；触及事件协议或 API 的项目（P2-6）另跑前端 `pnpm lint` + `pnpm build`。
- 测试不依赖真实智谱/Tavily 请求；真实接口只做无副作用的 smoke 验证（如 P0-2 的 usage 字段）。
- 每完成一项，更新本表状态列，并在根 README 的"已知问题/当前结构"同步对应变化。
