# demo-agent

一个从零手写 LLM Agent 的学习项目。不依赖任何 Agent 框架，直接用 OpenAI Python SDK 调用[智谱的 OpenAI 兼容接口](https://docs.bigmodel.cn/cn/guide/start/model-overview)（Chat Completions），从「一次性问答」做到「带工具调用的流式多轮对话」，工具涵盖文件读写与联网搜索。

## 环境

- Python 3.12+，依赖用 [uv](https://github.com/astral-sh/uv) 管理
- 核心依赖：`openai`、`python-dotenv`、`tavily-python`（联网搜索）

### 一条命令初始化前后端

在项目根目录执行：

```bash
make init
```

该命令会：

- 若不存在则从 `server/.env.example` 创建 `server/.env`（绝不覆盖已有配置）
- 在 `server/` 执行 `uv sync --locked`
- 在 `web/` 执行 `pnpm install --frozen-lockfile`

随后填写项目 `server/` 目录下的 `.env`：

```
API_KEY=xxxxxxxx.xxxxxxxx        # 智谱开放平台 API 密钥
BASE_URL=                        # 留空使用智谱兼容接口；可换成其他 OpenAI 兼容网关
MODEL=glm-4.6                           # 可按平台支持的模型替换
SYSTEM_PROMPT=你是一个有用的助手，可以调用工具来帮助用户。
TOOL_PROFILE=local                      # 本地教学用 local；公开演示用 public
TAVILY_API_KEY=tvly-xxx        # 仅联网搜索工具需要
```

`MODEL` 和 `SYSTEM_PROMPT` 留空或不设置时，会分别使用当前代码中的默认值。

初始化完成后，在项目根目录启动网页模式：

```bash
make dev
```

这会同时启动后端 `http://127.0.0.1:8000` 与前端；按 `Ctrl+C` 会停止两个进程。

如需分别调试，也可以在两个终端运行：

```bash
make dev-backend
```

```bash
make dev-frontend
```

浏览器访问 Vite 输出的地址（默认 `http://localhost:5173`）。

## 与 ReAct 的关系

这套 agent loop 本质上是 **ReAct 的工程化后继**。ReAct（Yao et al. 2022）的核心主张是「推理与行动交替进行」，而不是先想完再一次性执行——这个交替骨架与本项目的自循环完全一致：

```
模型决定动作 → 代码执行 → 结果回灌上下文 → 模型再决定 → …… → 模型给出最终答复
```

区别在于 **Action 和 Observation 的载体**：原始 ReAct 靠文本约定，现在靠协议字段。

| 维度 | 原始 ReAct | 本项目（function calling loop） |
|---|---|---|
| Action 载体 | 模型输出的**纯文本** | API 原生 assistant `tool_calls` 字段 |
| 解析方式 | 正则 / 字符串切分 | 直接取字段 + `json.loads` |
| 格式跑偏 | 解析失败，要重试或兜底 | schema 约束，基本不会 |
| Thought | **强制显式**输出 | 没有 |
| Observation | 拼回 prompt 文本 | `role="tool"` 消息 |
| 单步动作数 | 一次一个 | 一次可多个（并行调用） |
| 模型要求 | 任何文本生成模型 | 需支持 function calling |
| Token 成本 | Thought 占额外输出 | 无这部分开销 |
| 可调试性 | 推理过程肉眼可读 | 只看得到调了什么，看不到为什么 |

**ReAct 的形态**——一切都在文本里，代码要当解析器：

```
Thought: 用户问天气，我需要调用天气工具
Action: get_weather["北京"]
Observation: 北京今天晴，最高气温38℃      ← 代码执行后把这行拼回 prompt
Thought: 信息够了，可以回答
Answer: 北京今天晴，38℃
```

代码得负责：切出 `Action:` 那行、解析工具名与参数、把 `Observation:` 拼进去、发现 `Answer:` 就停止。任何一步格式变形都会崩。

**本项目的形态**——结构由协议保证：

```python
message = response.choices[0].message
message.tool_calls[0].function.name        # "get_weather"
message.tool_calls[0].function.arguments   # '{"city":"北京"}'
```

停止条件也是结构化的：`if not message.tool_calls`，不用去猜模型有没有写 `Answer:`。

#### 少掉的那部分：显式 Thought

ReAct 强制模型每步先写出推理再行动，本项目没有这个要求。想补上有两条路：

1. **system prompt 要求**：「调用工具前先用一句话说明为什么」。模型可以在 `delta.content` 里先产出说明，再给出 tool calls；现有文本增量分支会把这段说明实时显示出来。改一行 prompt 就能试，代价是每轮多花些 token
2. **加一个 `think` 工具**：实现为空，只把参数记录下来，让模型「调用」它来落地推理。听起来奇怪，但实测对复杂任务有帮助，因为它把推理变成了一次显式动作

注意推理模型（GLM 深度思考等）的 `reasoning_content` 与 ReAct 显式 Thought 不是一回事：前者的内部推理文本不属于正式回复，本项目跳过不展示；后者是模型主动输出的可见文本，可审、可干预。

## 当前结构

前后端各占一个目录，依赖各自独立管理（`server/` 用 uv，`web/` 用 pnpm），不引入 workspace 工具。

```
demo-agent/
├── server/                  # 后端：Agent 本体（Python / uv）
│   ├── api/                 # FastAPI 应用包（uvicorn api:app）
│   │   ├── __init__.py      # 组装并导出 app
│   │   ├── routes.py        # HTTP 路由、会话并发保护与 SSE 传输映射
│   │   ├── chat_stream.py   # 聊天 SSE 编排：事件循环、done 落库与埋点落库
│   │   └── deps.py          # 数据库入口、路径常量与会话运行锁
│   ├── database/            # SQLite schema 与 Session/Message/File 数据访问
│   ├── documents/           # PDF 隔离存储与文件生命周期清理
│   ├── agent/               # 模型交互
│   │   ├── client.py        # 智谱（OpenAI 兼容）客户端、MODEL、SYSTEM_PROMPT
│   │   ├── context_budget.py # 上下文预算：截断策略，构造 items 时的长度控制
│   │   ├── loop.py          # Chat Completions 流式 agent loop，产出项目内事件
│   │   └── metrics.py       # 运行埋点：TurnRecorder 内存账本（loop 记账不碰库）
│   ├── tools/               # 工具集合，一个工具一个文件，按领域分子包
│   │   ├── calculate.py
│   │   ├── get_weather.py
│   │   ├── files/           # 文件类（paths.py 做路径校验）
│   │   └── web/             # 联网类（client.py 含截断与不可信标注）
│   └── .env                 # API key 与 base url
└── web/                     # Vite + React 前端
    └── src/
        ├── adapter/         # 调后端、解析 SSE、还原项目事件
        ├── chat/            # 前端消息模型、历史映射与流式事件更新纯函数
        ├── hooks/           # 当前会话消息加载、发送与会话列表请求
        ├── components/      # 侧边栏、消息展示和输入框
        └── stores/          # 页内输入状态与当前 Session 指针
```

文件工具的根目录限定在 `server/` 内——Agent 读写不到 `web/` 与仓库根，`.env`、`.git` 与 `.sessions` 也禁止访问。会话、最终用户/助手消息、上传文件信息和消息—文件关系存于 `server/.data/demo-agent.sqlite3`；每轮 Chat 根据 `session_id` 与 `parent_message_id` 从 SQLite 还原当前消息链，再临时投影成 Chat Completions `items`。历史接口按本轮用户消息关联 `agent_turns` 与 `tool_runs`，在对应助手回复下展示工具名称、耗时及参数/结果短文本（最多约 500 字）；实时事件仍有完整结果。历史账本不记录工具与正文的交错位置，故历史界面在正文后展示工具卡片；模型上下文仍只使用最终用户/助手文本。

现有工具：

| 工具 | 作用 |
|---|---|
| `calculate` | 计算数学表达式 |
| `get_weather` | 查天气（写死的假数据） |
| `read_file` | 读 server 目录内文件 |
| `write_file` | 写 server 目录内文件，自动建父目录，已存在则覆盖 |
| `web_search` | 联网搜索，返回标题、链接、摘要 |
| `fetch_url` | 抓取网页正文 |
| `parse_attached_document` | 解析当前会话的 PDF 附件，按页提取文本存入 pages.json |

工具暴露由 `TOOL_PROFILE` 控制：

- `local`（默认）保留全部教学工具，仅适合可信本地环境。
- `public` 使用显式白名单，目前只向模型暴露 `get_weather` 与 `web_search`；`calculate`、`read_file`、`write_file`、`fetch_url`、`parse_attached_document` 同时从模型 schema 和执行分发中移除。
- `read_file` 在敏感文件策略完善前不进入公开模式；`fetch_url` 在补齐 scheme、重定向及内网地址防护前不进入公开模式。新增公开工具必须显式加入白名单并补测试。

关键函数的分工：

| 函数 | 职责 |
|---|---|
| `stream_events(items, max_turns, context)` | 请求 Chat Completions 接口，逐个执行模型发起的工具调用并产出事件；`context` 携带本轮会话信息供工具使用 |
| `execute_tool(name, args, context)` | 按名称执行一个本地工具，把工具异常转成可回灌的文本结果 |

`stream_events` 是唯一的 Agent 内核。它把智谱（OpenAI 兼容协议）的流式 chunk 翻译成项目自己的 `text_delta`、`tool_call`、`tool_result`、`done`、`max_turns` 与 `error`；FastAPI 层把同一事件编码成 SSE。这样前端不用理解模型的完整流式协议，后端内部也不掺杂打印或页面逻辑。

每次 SSE 响应必须以 `done` 或 `error` 结束：出现 `max_turns` 时 API 层会紧随其后追加一帧 `error` 终止；`stream_events` 抛错或未产出终止事件时，API 层同样输出 `error` 再结束。前端 `readSse` 把没有终止事件的 EOF 视为响应中断。整轮回复有总时限（`MAX_RUN_SECONDS`，默认 300 秒），模型网络连接或读取等待上限为 60 秒且禁用 SDK 自动重试，`web_search` / `fetch_url` 各最多等待 30 秒；超时后以 `error` 结束，并释放该会话的运行标记。

## 踩过的坑

- **编辑器没有导入补全**：pyright 默认用 PATH 里的系统 python，不会自动探测项目里的 `.venv`。在 `pyproject.toml` 加 `[tool.pyright]` 的 `venvPath = "."` 和 `venv = ".venv"` 解决
- **流式工具参数必须按 chunk 聚齐再解析**：`delta.tool_calls` 的 `arguments` 是跨多个 chunk 的增量，要按 `index` 拼接完整后才能 `json.loads`；解析失败时不能把只有调用、没有结果的半截历史留在上下文里
- **工具结果必须带原始 `tool_call_id`**：`role="tool"` 消息没有正确关联调用时，下一次请求会直接失败
- **工具输出统一成字符串**：`eval()` 可能返回 `int`，而当前工具事件都按字符串处理，所以 `calculate` 的返回值要 `str()` 包一层

## Session、消息与 PDF 上传

前端用 `crypto.randomUUID()` 生成 `session_id`，不单独创建空会话。第一次 `POST /api/chat` 携带 `parent_message_id: null` 时，后端在同一事务中创建 Session、用户消息及附件关系；后续请求携带上一轮 SSE 返回的消息 ID。`GET /api/sessions` 返回历史摘要，`GET /api/sessions/{session_id}` 返回消息及其附件。`user_message` 事件会在模型执行前返回已持久化的用户消息 ID，因此模型失败后仍可从该节点继续；`done` 事件返回助手消息 ID。

聊天输入框左下角的回形针用于选择单份 PDF，Tooltip 会说明只支持 PDF 和 10 MiB 上限。前端先检查类型、空文件与大小，选择后自动请求 `POST /api/documents`；服务端再次检查扩展名、MIME、`%PDF-` 文件头与实际读取大小。成功返回 `file_id`，原文件保存到 `server/.data/files/<file_id>/original.pdf`，上传元数据只存 SQLite，不再创建 `metadata.json`。Chat 请求通过 `ref_file_ids` 将附件绑定到具体用户消息，MVP 最多一项。

`uploaded` **仅表示保存成功**：当前尚无解析、检索、引用或基于附件的回答。输入框移除只取消客户端请求或移除草稿引用，不会立即删除已保存文件。启动时以及 `uv run python -m documents.cleanup` 会清理超过 1 小时的临时文件、超过 24 小时且从未被消息引用的文件，以及超过 24 小时且 SQLite 无记录的孤立目录；已被消息引用的文件不会按此规则回收。

这是可信本地演示，不能直接公开部署。应用读取 `UploadFile` 前，multipart 处理可能已占用临时空间；公网入口仍需要网关请求体上限、用户身份、配额和更完整的文件生命周期。`%PDF-` 文件头只是初筛，不代表 PDF 结构有效或安全。

## 已知问题

- `tools/calculate.py` 用 `eval()` 执行模型给的表达式，等于任意代码执行；权限与审批机制移除后模型无需确认即可触发，仅适用于本地学习，不能上线
- **prompt injection 未真正防住**：`web_search` / `fetch_url` 引入的外部内容可能夹带指令，且现在没有任何写入前确认，模型可能被诱导改写文件
- `write_file` 无确认直接覆盖文件，没有备份或事务，也还没有“读取外部内容后禁止写入”等隔离
- 上下文长度控制目前只有纯截断（`agent/context_budget.py`，`len(text)` 一字一 token 保守估算、预算 24K、system 永在、至少保留最新一条）；被截掉的早期历史对模型不可见，滚动摘要尚未实现
- 没有应用层重试策略；模型请求或流处理异常会转成 `error` 事件，但中断的任务不会自动续跑。可观测性埋点已上线（`agent/metrics.py` + `agent_turns`/`tool_runs` 表 + `chat_messages.meta`）：每轮记录模型请求耗时、流式 usage（智谱接口已验证支持 `include_usage`）与工具执行名/耗时/成败，落库失败只记日志不影响回复；stats 查询接口已上线（GET /api/sessions/{id}/stats 现场聚合），前端已展示会话 stats 和工具执行耗时
- 300 秒整轮时限只在模型 chunk 边界与工具执行前后检查；已进入阻塞的工具调用无法被强行抢占，超时要等调用返回后才生效
- HTTP 聊天用进程内标志与锁阻止并发运行；多 worker 或多进程部署不受支持

## 后续计划

### 1. 上下文控制

截断已上线（`agent/context_budget.py`：预算 24K、system 永在、至少保留最新一条）。滚动摘要和压缩分隔线留待下一 PR；工具过程历史展示读取 `tool_runs`，模型上下文仍只有最终用户/助手文本。

### 2. 并发与会话存储

- SQLite 已保存 Session、最终消息、文件及消息—文件关系；当前没有用户账号或跨用户权限模型
- 同一 Session 用进程内集合拒绝并发 Chat；不同 Session 可并行，但多 worker / 多进程无法共享这把运行锁
- 客户端断开后用户消息可能已持久化，模型执行是否继续取决于生成器生命周期；尚无任务恢复机制

### 3. 记忆系统（跨会话记忆）

多会话解决的是「不同对话互不干扰」，记忆解决的是「换个会话它还记得我」。

- 从「会话内上下文」升级为「跨会话可检索的知识」：把值得长期保留的信息抽出来单独存
- 需要决定写入时机（每轮自动抽取 vs 模型主动调 `remember` 工具）和召回方式（关键词检索 vs 向量检索）
- 召回结果可以作为 message Item 或工具结果注入，两种做法的可控性和 token 成本不同

### 4. MCP 支持

MCP（Model Context Protocol）是工具接入的标准协议。接一个 MCP client 之后，任何 MCP server 提供的工具都能直接用，不必再为每种能力手写一个模块。

- 主要改造点：现在 `TOOLS` / `TOOL_HANDLERS` 是 import 时**静态**生成的，MCP 工具得在运行时从 server 的 `list_tools` 拉取后**动态**合并进来
- schema 转换几乎是平移：MCP 的 `inputSchema` 就是 JSON Schema，组合成 `{"type":"function","name": ..., "parameters": inputSchema}` 即可；执行时把 `execute_tool` 的分发指向 server 的 `call_tool`
- 要处理 stdio 与 HTTP 两种传输方式、连接的建立与释放、以及工具名冲突（多个 server 可能有同名工具）
- 安全上：外部 MCP server 与网页内容同属不可信来源，它返回的结果也应当加标注

### 5. Skill 机制

Skill 是把「某类任务该怎么做」写成文档，按需加载进上下文——扩展的是**知道怎么做**，而工具扩展的是**能做什么**。

- 与工具的本质区别：skill 不执行任何代码，它只是提示词层面的能力包，最终还是靠已有工具去落地
- 存放约定：`skills/<name>/SKILL.md` 加一份元数据（名称 + 一句话描述 + 触发场景）
- 加载策略：启动时只把各 skill 的**名称与描述**放进上下文，模型判断需要时再读全文——这一步正好复用现成的 `read_file`
- 难点是触发时机与上下文预算的平衡：全部预加载会挤占上下文，全靠模型自觉又常常该用不用；描述写得够不够准，直接决定命中率
