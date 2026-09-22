# AGENTS.md

本文件适用于整个仓库。用户的明确要求优先；若子目录以后出现更近的 `AGENTS.md` 或 `AGENTS.override.md`，以更近的文件为准。

## 项目定位与事实来源

- 这是一个教学型 Agent 项目：不依赖 Agent 框架，直接使用 OpenAI Python SDK 调用智谱的 OpenAI 兼容接口（Chat Completions）。
- 后端位于 `server/`，使用 Python 3.12、FastAPI、OpenAI SDK、Tavily 和 uv。
- 前端位于 `web/`，使用 React、TypeScript、Vite 和 Tailwind CSS；assistant-ui 已移除，界面由使用者自行重写。
- 实际后端入口是 `server/api.py`（HTTP/SSE）；实际依赖清单是 `server/pyproject.toml` 与 `server/uv.lock`。
- 根目录的 `main.py`、`pyproject.toml` 和 `.python-version` 是早期空脚手架，不是后端运行环境。
- 当前代码与依赖清单是实现事实来源。架构、命令、事件协议或已知限制改变时，同步更新相关 README。

## 开始改动前

- 先查看 `git status --short`，识别并保留用户已有改动。本仓库可能处于脏工作树；不要顺手格式化、删除或重写任务范围外的文件。
- 不直接查看、打印、修改或提交 `server/.env` 的内容。应用通过 `load_dotenv()` 正常加载配置不受此限；配置文档只引用 `server/.env.example` 和占位值。
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

- `server/agent/`：智谱（OpenAI 兼容）客户端配置与唯一 Agent 内核。
- `server/tools/`：工具 schema、实现、注册和执行分发。
- `server/api.py`：FastAPI 路由、进程内会话运行保护、CORS 和项目事件到 SSE 的传输映射；`server/database/` 持久化 Session/Message/File。
- `web/src/adapter/`：HTTP 请求、SSE 分帧与项目事件类型定义，不依赖任何 UI 框架。`types.ts` 是事件类型，`transport.ts` 是 API 地址、请求错误与 SSE 分帧，`index.ts` 是聊天请求与公共导出。
- `web/src/App.tsx`：界面组合层；当前为重写占位，展示逻辑与 `adapter/` 保持分离。

保持模型传输、Agent 编排、工具实现、会话存储和网页呈现分离。不要在 `stream_events` 中打印、读终端输入、生成 SSE 帧或依赖 React 语义。

## 模型协议与 Agent loop 不变量

- 生产代码使用 `client.chat.completions.create(...)`（OpenAI 兼容协议，默认指向智谱 `https://open.bigmodel.cn/api/paas/v4`）。不要改回 Responses API 的 `input` / `function_call` Item 协议，除非任务明确要求迁移设计。
- `server/agent/loop.py::stream_events(items, max_turns, context)` 是唯一 Agent 内核。`items` 是 Chat Completions messages 列表；它产出结构化项目事件，并原地追加 `items`；API 的上下文就是这份列表。`context` 携带本轮会话信息（`tools/context.py::SessionContext`），供 `parse_attached_document` 这类需要定位会话附件的工具使用，不进入事件流。
- 每次模型请求保留 `stream=True`。每轮由 SQLite 的父消息链构造临时 `items`；Agent loop 不直接读写数据库。
- 流中只把 `delta.content` 映射为文本增量；`delta.reasoning_content`（GLM 深度思考）不属于用户可见文本，跳过不展示、不持久化。
- 流式工具参数必须按 `index` 把 `delta.tool_calls` 的增量聚齐后再解析。非法 JSON 属于编排错误，应成为 `error` 事件，且不能把 assistant tool_calls 消息留在历史里（只有调用、没有结果的半截历史）。
- 一条 assistant 消息携带本轮全部工具调用，整体追加到上下文；不能只保留文本或部分调用。
- 同一响应可以包含多个并行 tool calls。当前实现按响应顺序逐个执行并提交结果，副作用的实际发生顺序等于模型调用顺序。
- 每个工具结果使用 `role="tool"` 消息，并原样复用对应调用的 `tool_call_id`。不得漏掉或重复回填。
- 工具处理器自身的异常由 `execute_tool` 转成字符串结果，让模型可以修正重试；请求错误、流错误、参数 JSON 错误等编排异常由 `stream_events` 转成 `error` 事件。不要混淆两条失败路径。
- `max_turns` 限制一次用户任务内的模型请求次数。达到上限产出 `max_turns`，不能让工具循环无限运行。

## 项目事件、SSE 与前端映射

稳定 SSE 事件及字段为：

- `user_message`：`message_id`（API 持久化用户消息后追加）
- `text_delta`：`text`
- `tool_call`：`id`、`name`、`args`
- `tool_result`：`id`、`content`
- `done`：`content`、`message_id`（API 持久化助手消息后追加）
- `max_turns`
- `error`：`message`

修改名称、字段、顺序或终止语义时，至少同步检查：

- `server/agent/loop.py`
- `server/api.py`
- `server/tests/test_agent_loop.py`
- `web/src/adapter/`
- `web/src/App.tsx` 及 UI 组件（若事件呈现语义变化）
- 根 README 的事件与架构说明

模型网关到后端是 Chat Completions chunk 流；后端到消费者是项目自己的事件协议。不要把模型原始 chunk 直接暴露给浏览器。

- 聊天接口是带 JSON body 的 POST SSE，前端必须使用 `fetch` + `ReadableStream`，不能改用不支持该请求形状的 `EventSource`。
- 后端 SSE 帧保持 `data: <json>\n\n` 和 `text/event-stream`。前端 `readSse` 的 `buffer` 用于还原网络拆开的半帧或合并的多帧，不能按单个 chunk 直接 `JSON.parse`。
- UI 层自行消费 `adapter/` 的事件流；`text_delta` 是增量，需要完整文本时由展示层自行累加。
- 保持事件时间顺序：文本增量按到达顺序累加，遇到 `tool_call` 结束当前文本段，后续文本另起新段，`tool_result` 按同一 `id` 关联对应调用。不要把工具前后的文本过滤、合并或统一挪到末尾。
- `done.content` 是完整最终文本的事实来源，但当前消费者用 streamed text 呈现；若开始消费它，避免重复显示已经收到的 deltas。
- 流式 `error` 要先形成用户可见状态，再终止 adapter 的运行状态。不要只抛异常而丢失后端错误信息。

## 工具约定与安全边界

当前工具是 `calculate`、`get_weather`、`parse_attached_document`、`read_file`、`write_file`、`web_search` 和 `fetch_url`。新增或修改工具时：

- 每个工具模块导出扁平 function schema `SCHEMA`（`type`、`name`、`description`、`parameters`、`strict` 同层）和 `run(args)`；Chat Completions 的 `function` 外壳由 `loop.py` 在请求时统一添加，不要写进各工具的 `SCHEMA`。
- 在所属子包的 `MODULES` 中登记工具；根 `server/tools/__init__.py` 从模块生成 Schema 与处理器，不要再维护一份手写名称映射。
- 工具最终返回字符串。对模型生成的参数验证类型、必填字段、长度、允许值和副作用边界；JSON 能解码不代表它一定是对象或安全输入。
- 为成功和失败路径补测试，尤其是路径、外部 URL、覆盖写入及部分执行失败。不要使用真实密钥或真实网络作为单元测试前提。

已知高风险边界：

- `calculate` 直接对模型输入使用 `eval()`，等同任意 Python 代码执行，仅是教学遗留。项目当前没有任何审批或权限机制，模型每次调用都会直接执行；不要把它描述为安全计算器、部署到不可信用户环境或扩大其暴露面；安全化时改用受限表达式解析并补恶意输入测试。
- 文件工具的根目录是 `server/`，不是仓库根。必须保留真实路径规范化、越界和符号链接逃逸检查。
- 现有路径黑名单阻止首段为 `.env`、`.git` 或 `.sessions` 的路径，但仍不等于全面的敏感文件策略。不要在文档中夸大现有保护；触碰文件工具安全时应覆盖绝对路径、`..`、符号链接、敏感目录和覆盖行为测试。
- `write_file` 会创建父目录并直接覆盖现有文件，当前没有备份、确认或跨进程事务，模型可以在无任何确认的情况下改写文件。扩大作用域或自动化程度前，必须评估数据损失与 prompt injection 风险。
- `web_search` 和 `fetch_url` 的结果是外部不可信内容。保留数量/长度限制、边界标记和提示注入警告；这些标记只是缓解，不是安全隔离。
- 若更改 URL 抓取方式，显式验证 scheme、重定向和内网/本机地址，评估 SSRF；不要把网页中的指令当作应用指令执行。
- 真实 API smoke test 只能使用无副作用请求，不能触发 `write_file`、危险表达式或其他破坏性工具。

## 会话格式与并发

- Session、最终用户/助手消息、上传文件与消息附件关系持久化于 SQLite；Chat 根据父消息链临时构造以 system message 开始的 `items`。
- 工具调用和工具结果仍只属于本轮临时 `items`，尚未落库；不得把它们拆成只有调用、没有结果的半截上下文。
- `user_message` SSE 先返回已持久化用户消息 ID；`done` 携带助手消息 ID。错误或客户端取消后用户消息可能保留，下一轮应以该 ID 为父节点。
- `api.py` 用进程内集合与锁阻止同一 Session 并发 Chat；进程内锁不解决多 worker 或多进程竞争，不要描述为跨进程安全。

## 编码、依赖与文档

- 延续现有简洁、显式的实现；除非任务明确需要，不引入 Agent 框架或新的生产依赖。
- 前端代码风格：`if` 语句必须使用花括号，单行 `if (...) return/throw/break` 写法一律展开为多行。
- 用户可见文本和主要说明使用中文；新增提示、错误和文档保持一致。
- 后端依赖变化同时更新 `server/pyproject.toml` 和 `server/uv.lock`；前端依赖变化同时更新 `web/package.json` 和 `web/pnpm-lock.yaml`。
- 前端的 `@/*` alias 必须在 TypeScript 与 Vite 配置中保持一致。界面重写时保持展示逻辑与 `adapter/` 的传输职责分离。
- 浏览器页内持有当前 Session ID 与父消息 ID，刷新后默认新建空会话；历史可通过 `GET /api/sessions` 和 `GET /api/sessions/{id}` 从 SQLite 查询。
- README 描述当前结构与用法。重构时更新“当前结构”和“已知问题”。

## 验证矩阵

- 只改文档：检查路径、命令、字段和当前代码一致；无需把未运行的测试写成已通过。
- 修改 Agent loop、工具协议或会话格式：运行完整后端 unittest。
- 修改 API、SSE 或项目事件：运行完整后端 unittest，并运行前端 `pnpm lint` 与 `pnpm build`；同时补充中断、错误和事件顺序测试。
- 修改前端：至少运行 `pnpm lint` 与 `pnpm build`。仓库当前没有前端测试脚本；涉及文本—工具—文本呈现时必须人工验证文本段不丢失、不合并、不重排。
- 修改 Python 依赖或锁文件：运行 `uv lock --check` 和受影响的后端测试。
- 修改前端依赖或锁文件：使用锁定安装语义，并运行 `pnpm lint` 与 `pnpm build`。
- 测试不得依赖真实智谱/Tavily 请求。使用 mock 构造 chunk 流、同轮多个 tool calls 和错误路径。
- 相关后端测试应覆盖成功文本、chunk 增量聚齐、同轮全部调用、原始 tool_call_id、最大轮次、error 和无效 JSON 参数。
- 凭证或网络受限时，明确报告未验证的真实网关边界；不要把 mock 单元测试称为真实 API smoke test。

## Code Review 优先级

审查时优先标记：

- 任意代码执行、无确认覆盖写入或敏感文件访问。
- 丢弃同轮任一 tool call，或解析失败后遗留只有调用、没有结果的半截 assistant 消息。
- `role="tool"` 消息的 `tool_call_id` 不匹配、漏处理同轮调用，或改变有副作用工具的执行顺序。
- 把文本 delta 当完整文本、重新手拼工具参数 delta，或让 SSE/前端事件联合类型漂移。
- 前端把工具前后的多段文本合并、过滤或重排，或 tool result 更新到错误的调用。
- 错误、取消或最大轮次路径导致内存会话中的 call 与 output 不配对。
- 并发 chat 的运行标志泄漏，或路径逃逸、真实密钥/会话泄漏、未标记的外部内容、SSRF 或把提示注入内容当指令执行。
