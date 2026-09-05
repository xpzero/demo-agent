# AGENTS.md

本文件适用于整个仓库。用户的明确要求优先；若子目录以后出现更近的 `AGENTS.md` 或 `AGENTS.override.md`，以更近的文件为准。

## 项目定位与事实来源

- 这是一个教学型 Agent 项目：不依赖 Agent 框架，直接使用 OpenAI Python SDK 的 Responses API。
- 后端位于 `server/`，使用 Python 3.12、FastAPI、OpenAI SDK、Tavily、PostgreSQL、Psycopg 3 和 uv。
- 前端位于 `web/`，使用 React、TypeScript、Vite、assistant-ui，以及当前工作树中的 Tailwind CSS / Radix 风格组件。
- 实际后端入口是 `server/main.py`（CLI）和 `server/api.py`（HTTP/SSE）；数据库 migration 入口是 `server/migrate.py`；实际依赖清单是 `server/pyproject.toml` 与 `server/uv.lock`。
- 根目录的 `main.py`、`pyproject.toml` 和 `.python-version` 是早期空脚手架，不是后端运行环境。
- 当前代码与依赖清单是实现事实来源。根 README 同时包含历史演进示例；不要把旧版本片段误当成当前实现。架构、命令、事件协议或已知限制改变时，同步更新相关 README。

## 开始改动前

- 先查看 `git status --short`，识别并保留用户已有改动。本仓库可能处于脏工作树；不要顺手格式化、删除或重写任务范围外的文件。
- 不直接查看、打印、修改或提交 `server/.env` 的内容。应用通过 `load_dotenv()` 正常加载配置不受此限；配置文档只引用 `server/.env.example` 和占位值。
- `server/.sessions/` 含完整对话、工具结果和 encrypted reasoning，属于敏感运行时数据；除非任务明确要求，不读取、不改写、不提交。
- PostgreSQL `sessions` 表及数据库备份包含相同敏感数据。测试只使用独立的 `TEST_DATABASE_URL`，不得连接、清空或迁移开发/生产数据库。
- 不编辑或提交 `.venv/`、`node_modules/`、`web/dist/`、`__pycache__/`、缓存或其他生成物。
- 除非用户明确要求，不创建提交、不切换或新建分支、不推送远端。

## 初始化、运行与检查

初始化仅在需要安装依赖或首次配置时从仓库根执行：

```bash
make init
```

`make init` 会在缺少时创建 `server/.env`，并分别执行锁定安装；它不是日常验证命令，不能覆盖已有 `.env`。

从仓库根启动：

```bash
make dev
make dev-backend
make dev-frontend
make db-migrate
make test-backend
```

运行 CLI：

```bash
cd server
uv run main.py
```

后端检查必须从 `server/` 执行：

```bash
uv run python -m unittest discover -s tests -v
uv lock --check
```

前端检查必须从 `web/` 执行：

```bash
pnpm lint
pnpm build
```

Makefile 没有 `test`、`lint`、`build` 或 `check` 目标。不要为了运行后端命令改用根目录的空 Python 环境，也不要在仓库根直接运行前端脚本。

## 架构边界

- `server/agent/`：OpenAI 客户端配置与唯一 Agent 内核。
- `server/tools/`：工具 schema、实现、注册和执行分发。
- `server/sessions/`：Items 会话模型、SessionService、PostgreSQL 持久化、旧 JSON 导入和 CLI 会话命令。
- `server/cli/`：终端输入循环与事件呈现。
- `server/api.py`：FastAPI 路由、CORS 和项目事件到 SSE 的传输映射。
- `web/src/adapter.ts`：HTTP 请求、SSE 分帧及项目事件到 assistant-ui message parts 的映射。
- `web/src/App.tsx` 与 `web/src/ToolCallPart.tsx`：消息和工具调用的 UI 组合；`web/src/preview.ts` 只提供开发预览夹具。

保持模型传输、Agent 编排、工具实现、会话存储和 CLI/网页呈现分离。不要在 `stream_events` 中打印、读终端输入、生成 SSE 帧或依赖 React 语义。

## Responses API 与 Agent loop 不变量

- 生产代码使用 `client.responses.create(...)`；不要重新引入 Chat Completions 的 `messages` / assistant `tool_calls` / `role="tool"` 协议。
- `server/agent/loop.py::stream_events(items, services, max_turns, on_approval, session_id)` 是唯一 Agent 内核。它产出结构化项目事件，并原地追加 `items`；CLI、API 和会话保存依赖这一行为。
- 每次模型请求保留 `stream=True`、`store=False` 和 `include=["reasoning.encrypted_content"]`。若改成 `previous_response_id`、Conversations API 或服务端存储，必须同时重新设计持久化、隐私语义、测试和 README。
- `reasoning.encrypted_content` 是供后续请求续传的不透明数据，必须随完整 Items 原样保留和持久化。不要尝试解析、单独写入日志或向用户展示，也不要把它包装成用户可见的“思考过程”。当前项目没有 reasoning 事件。
- 流中只把 `response.output_text.delta` 映射为文本增量；以 `response.completed.response` 作为完整 Response 的事实来源。显式处理 `response.failed`、`response.incomplete`、流内 `error` 和“流结束但没有 completed”。
- 不手工拼接 `response.function_call_arguments.delta`。当前产品不展示参数生成过程，应在 completed 后读取完整 `function_call.arguments`。
- 在改变历史前先解析完同一响应内所有调用参数。非法 JSON 属于编排错误，应成为 `error` 事件，不能留下只有 call、没有 output 的半截历史。
- 每轮把完整 `response.output` 追加到上下文，不能只保留 `output_text`、message 或 function call；其中可能同时包含 message、reasoning 和其他 Item。
- 同一 `response.output` 可以包含多个 sibling function calls。当前实现按响应顺序检查全部调用，并按原顺序提交结果；`allow` 会在准备批次时执行，`ask` 则在用户逐个批准时执行，因此副作用的实际发生顺序不一定等于模型调用顺序。若任务要求严格顺序，必须重新设计执行阶段，不能把结果顺序误当成执行顺序。
- 每个工具结果使用 `function_call_output`，并原样复用对应调用的 `call_id`。不得用 Item `id` 代替 `call_id`，不得漏掉或重复回填。
- 工具处理器自身的异常由 `execute_tool` 转成字符串结果，让模型可以修正重试；请求错误、流错误、参数 JSON 错误等编排异常由 `stream_events` 转成 `error` 事件。不要混淆两条失败路径。
- `max_turns` 限制一次用户任务内的模型请求次数。达到上限产出 `max_turns`，不能让工具循环无限运行。

## 项目事件、SSE 与前端映射

稳定项目事件及字段为：

- `text_delta`：`text`
- `tool_call`：`id`、`name`、`args`
- `tool_result`：`id`、`content`，拒绝或审批恢复结果还带 `outcome`
- `approval_required`：`call_ids`
- `done`：`content`
- `max_turns`
- `error`：`message`

修改名称、字段、顺序或终止语义时，至少同步检查：

- `server/agent/loop.py`
- `server/cli/render.py`
- `server/api.py`
- `server/tests/test_agent_loop.py`
- `web/src/adapter.ts`
- `web/src/App.tsx`、`web/src/ToolCallPart.tsx` 和相关 preview fixtures（若 message part 语义变化）
- 根 README 的事件与架构说明

OpenAI 到后端是 Responses typed event 流；后端到消费者是项目自己的事件协议。不要把 OpenAI 原始事件直接暴露给 CLI 或浏览器。

- 聊天接口是带 JSON body 的 POST SSE，前端必须使用 `fetch` + `ReadableStream`，不能改用不支持该请求形状的 `EventSource`。
- 后端 SSE 帧保持 `data: <json>\n\n` 和 `text/event-stream`。前端 `readSse` 的 `buffer` 用于还原网络拆开的半帧或合并的多帧，不能按单个 chunk 直接 `JSON.parse`。
- assistant-ui 每次 yield 需要“截至当前的完整 content 快照”，不是单个 delta。必须保留 `currentText += event.text` 和复制后的完整 parts。
- 保持原始 part 顺序：文本增量只累加到当前 text part；遇到 `tool_call` 后结束当前文本段并插入 tool-call part；后续文本创建新 text part；`tool_result` 按同一 `id` 更新对应 tool-call。不要把工具前后的文本过滤、合并或统一挪到末尾。
- `done.content` 是完整最终文本的事实来源，但当前消费者用 streamed text 呈现；若开始消费它，避免重复显示已经收到的 deltas。
- 流式 `error` 要先形成用户可见状态，再终止 adapter 的运行状态。不要只抛异常而丢失后端错误信息。
- `tool_call` 在等待审批时额外带 `approval_required: true`，`write_file` 还带结构化 `preview`。浏览器只通过项目事件接入审批，不直接消费 OpenAI 原始事件。

## 工具约定与安全边界

当前工具是 `calculate`、`get_weather`、`read_file`、`write_file`、`web_search` 和 `fetch_url`。新增或修改工具时：

- 每个工具模块导出扁平 Responses function schema `SCHEMA` 和 `run(args)`。`type`、`name`、`description`、`parameters`、`strict` 位于同一层，不套 Chat Completions 的 `function` 外壳。
- 每个工具模块还要导出 `permission_requests(args)`，用 `PermissionRequest` 描述本次具体操作；权限信息不能混入发给 Responses API 的 `SCHEMA`。需要预览的工具可以额外导出 `preview(args)`。
- 在所属子包的 `MODULES` 中登记工具；根 `server/tools/__init__.py` 从模块生成 Schema、处理器和权限请求构造器，不要再维护一份手写名称映射。
- 默认权限规则来自 `server/permission.json`。配置必须在启动时严格加载；无效 JSON、规则结构或 action 必须导致启动失败，不能静默降级为 `allow`。
- 工具最终返回字符串。对模型生成的参数验证类型、必填字段、长度、允许值和副作用边界；JSON 能解码不代表它一定是对象或安全输入。
- 为成功和失败路径补测试，尤其是路径、外部 URL、覆盖写入及部分执行失败。不要使用真实密钥或真实网络作为单元测试前提。

已知高风险边界：

- `calculate` 直接对模型输入使用 `eval()`，等同任意 Python 代码执行，仅是教学遗留，因此当前权限结果必须保持 `ask`。不要把它描述为安全计算器、部署到不可信用户环境或扩大其暴露面；安全化时改用受限表达式解析并补恶意输入测试。
- 文件工具的根目录是 `server/`，不是仓库根。必须保留真实路径规范化、越界和符号链接逃逸检查。
- 现有路径黑名单阻止首段为 `.env`、`.git` 或 `.sessions` 的路径，但仍不等于全面的敏感文件策略。不要在文档中夸大现有保护；触碰文件工具安全时应覆盖绝对路径、`..`、符号链接、敏感目录和覆盖行为测试。
- `write_file` 会创建父目录并直接覆盖现有文件，当前通过 Diff、内容摘要复核和单次确认阻止自动执行或过期审批，但仍没有备份或跨进程事务。扩大作用域、自动调用条件或权限前，必须评估数据损失与 prompt injection 风险。
- `web_search` 和 `fetch_url` 的结果是外部不可信内容。保留数量/长度限制、边界标记和提示注入警告；这些标记只是缓解，不是安全隔离。
- 若更改 URL 抓取方式，显式验证 scheme、重定向和内网/本机地址，评估 SSRF；不要把网页中的指令当作应用指令执行。
- 真实 API smoke test 只能使用无副作用请求，不能触发 `write_file`、危险表达式或其他破坏性工具。

## 会话格式、保存时机与并发

- `Session` 保存 `id`、`items`、`revision`，审批暂停时还保存 `pending_approval`；新会话以 system message Item 开始并立即写入 PostgreSQL。
- 加载器只接受 Items 格式。不要恢复旧 `messages`、assistant `tool_calls` 或 `role="tool"` 兼容层，除非任务明确要求一次新的迁移设计。
- SDK 返回的 Pydantic Items 存入 JSONB 前使用 `model_dump(exclude_none=True)`；从 PostgreSQL 加载的普通字典必须仍可作为 Responses input 重放。
- `SessionService.save()` 使用 revision 条件更新；成功提交后才推进内存对象的 revision，冲突时停止当前写入并重新加载数据库状态。
- 正常完成时保存配对完整的 function call 与 output；自动工具轮次在发送工具事件和下一次模型请求前 checkpoint。等待审批时允许暂存未配对调用，但 `pending_approval` 必须完整记录同轮所有调用。
- 文本 delta 继续实时发送；完整 `response.output` 写入成功后才发送 `done`。保存失败转成明确 error，并释放当前进程的运行状态。
- `server/api.py` 按明确的 `session_id` 获取和保存会话，并用进程内锁阻止同会话重复运行。浏览器和 CLI 分别维护自己选择的当前 Session，后端没有全局 `current`。
- 当前部署约定为单 worker。多 worker、CLI 与 API 竞争同一 Session 需要后续数据库租约；revision 只保护版本更新，不等同运行锁。
- API 单元测试注入 FakeSessionService；PostgreSQL 集成测试只读取独立 `TEST_DATABASE_URL`。旧 `server/.sessions/` 只作为显式迁移源和备份。

## 编码、依赖与文档

- 延续现有简洁、显式的实现；除非任务明确需要，不引入 Agent 框架或新的生产依赖。
- 用户可见文本和主要说明使用中文；新增提示、错误和文档保持一致。
- 后端依赖变化同时更新 `server/pyproject.toml` 和 `server/uv.lock`；前端依赖变化同时更新 `web/package.json` 和 `web/pnpm-lock.yaml`。
- 前端的 `@/*` alias 必须在 TypeScript 与 Vite 配置中保持一致。修改 assistant-ui message part 或工具 UI 时，以已安装版本的类型为准，不凭旧示例猜 API。
- 当前浏览器把一条后端会话 ID 保存在 `localStorage`，可以在刷新后恢复待审批卡或继续已完成决定的审批批次；它仍没有会话列表、切换 UI 或完整聊天历史恢复。不要仅因后端有这些 API 就把它们描述成前端现有能力。
- 数据库 schema 通过 `server/migrate.py` 和版本化 SQL 显式升级；应用启动只校验 schema。已执行 migration 文件不可修改，结构变化新增下一个版本。
- README 的历史教学价值需要保留。重构时更新“当前结构”和“已知问题”，不要无意删掉用于解释 V1-V8 演进的旧版示例。

## 验证矩阵

- 只改文档：检查路径、命令、字段和当前代码一致；无需把未运行的测试写成已通过。
- 修改 Agent loop、工具协议或会话格式：运行完整后端 unittest。
- 修改 API、SSE、项目事件或保存时机：运行完整后端 unittest，并运行前端 `pnpm lint` 与 `pnpm build`；同时补充中断、错误、事件顺序和保存目标测试。
- 修改 SessionService、SQL 或 migration：除完整 unittest 外，设置独立 `TEST_DATABASE_URL` 运行 PostgreSQL 集成测试，并执行 `uv run python migrate.py` 验证 migration。测试数据库名应使用清晰的 `_test` 后缀。
- 修改前端：至少运行 `pnpm lint` 与 `pnpm build`。仓库当前没有前端测试脚本；涉及文本—工具—文本映射时必须人工或新增测试验证 part 不丢失、不合并、不重排。
- 修改 Python 依赖或锁文件：运行 `uv lock --check` 和受影响的后端测试。
- 修改前端依赖或锁文件：使用锁定安装语义，并运行 `pnpm lint` 与 `pnpm build`。
- 测试不得依赖真实 OpenAI/Tavily 请求。使用 mock 构造 typed events、完整 Response、同轮多个 function calls 和错误事件。
- 相关后端测试应覆盖成功文本、完整 output 保留、同轮全部调用、原始 call_id、最大轮次、无 completed、failed/incomplete/error、无效 JSON 参数和 Items JSON 往返。
- 凭证或网络受限时，明确报告未验证的真实网关边界；不要把 mock 单元测试称为真实 API smoke test。

## Code Review 优先级

审查时优先标记：

- 任意代码执行、无确认覆盖写入、敏感文件访问或工具权限扩张。
- 丢弃 `response.output` 中的 reasoning/message Items，或解析失败后遗留半截 function call。
- `function_call_output.call_id` 不匹配、漏处理同轮调用，或改变有副作用工具的执行顺序。
- 把文本 delta 当完整文本、重新手拼工具参数 delta，或让 CLI/SSE/前端事件联合类型漂移。
- 前端把工具前后的多段文本合并、过滤或重排，或 tool result 更新到错误的调用。
- 错误、取消或最大轮次路径导致内存/落盘会话中的 call 与 output 不配对。
- 依赖全局 `SessionManager.current` 的交错保存、并发 worker 或并行 chat。
- 路径逃逸、真实密钥/会话泄漏、未标记的外部内容、SSRF 或把提示注入内容当指令执行。
