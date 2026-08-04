# demo-agent

一个从零手写 LLM Agent 的学习项目。不依赖任何 Agent 框架，只用 OpenAI SDK，逐步从「一次性问答」演进到「带工具调用的流式多轮对话」。

## 环境

- Python 3.12+，依赖用 [uv](https://github.com/astral-sh/uv) 管理
- 核心依赖：`openai`、`python-dotenv`

项目根目录创建 `.env`：

```
OPENAI_API_KEY=sk-xxx
OPENAI_BASE_URL=https://your-gateway
```

运行：

```bash
uv sync
uv run main.py
```

## 演进过程

### V1 · 单次连接

目标：打通链路，搞清响应结构。

```python
client = OpenAI()  # 自动读取 OPENAI_API_KEY / OPENAI_BASE_URL

response = client.chat.completions.create(
    model=MODEL,
    messages=[{"role": "user", "content": "Say Hi!"}],
)
print(response.choices[0].message.content)
```

要点：

- `OpenAI()` 不传参时从环境变量读 key 和 base_url，配合 `load_dotenv()` 即可
- 响应层级固定为 `choices[0].message.content`
- 这一版是**无状态**的：模型不记得任何历史，每次请求就是一次独立的函数调用

### V2 · 简单的 agent loop

目标：让模型能使用外部工具。

这一版引入两个新东西——**工具声明**和**循环**。

工具声明（`tools.py`）用 JSON Schema 描述工具签名，模型完全靠 `description` 判断该不该调、怎么传参：

```python
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "calculate",
            "description": "计算一个数学表达式的结果",
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {"type": "string", "description": "待计算的表达式"},
                },
                "required": ["expression"],
            },
        },
    },
]
```

循环的逻辑：

```
请求模型 ──► 返回 tool_calls？
              ├─ 是 → 执行工具 → 结果以 role="tool" 追加进 messages → 回到请求
              └─ 否 → 打印文本回复，结束
```

要点：

- **模型从不执行工具**，它只输出「我想调用 X，参数是 Y」的意图，真正执行的是本地代码
- 一轮工具调用会往 `messages` 里塞两类消息：带 `tool_calls` 的 assistant 消息，以及每个调用对应的 `role="tool"` 消息（必须用 `tool_call_id` 对应上）
- 必须循环而不是只处理一次：模型可能拿到工具结果后继续调下一个工具
- 用 `max_turns` 兜底，防止模型陷入无限调用

### V3 · 多轮对话

目标：跨轮次保留上下文，做成可交互的命令行 Agent。

结构变成**双层循环**：

```
外层 while：读用户输入 → 追加 user 消息 → 进内层
内层 while：agent loop（同 V2），直到模型给出文本回复 → 回外层等下一句
```

要点：

- 关键差别只有一个：`messages` 从「每次新建」变成「整个会话复用同一份」
- assistant 和 tool 消息都要留在上下文里，不能只留最终文本，否则模型会忘记自己调过什么工具
- `input().strip()` 去掉首尾空白，避免误判退出指令

### V4 · 流式输出

目标：文本边生成边打印，消除长回复的等待感。

只需给请求加 `stream=True`，但处理方式变化不小：

```python
for chunk in stream:
    delta = chunk.choices[0].delta

    if delta.content:                      # 文本 → 实时打印
        print(delta.content, end="", flush=True)

    for tc in delta.tool_calls or []:      # 工具调用 → 跨 chunk 拼接
        ...
```

要点：

- 流式下**没有现成的 message 对象**，要自己把 `content` 和 `tool_calls` 拼成一条 assistant 消息回填上下文
- 工具调用的 `arguments` 是被切成多个 chunk 逐段送来的，必须累加完整才能 `json.loads`
- ⚠️ 标准实现中每个工具调用有递增的 `index`，但**实测本项目使用的网关对多个调用一律返回 `index=0`**。按 index 归拢会把两个调用的参数拼成非法 JSON（报 `Extra data`）。改用「出现新的 `id` 就是一个新调用」来划分边界，对两种实现都成立

## 当前结构

```
demo-agent/
├── main.py      # 入口，只负责启动
├── agent.py     # 对话流程与 agent loop
├── tools.py     # 工具声明 + 实现
└── .env         # API key 与 base url
```

`agent.py` 内的分工：

| 函数 | 职责 |
|---|---|
| `execute_tool_calls` | 执行工具并把结果回填 messages，两种模式共用 |
| `run_tool_loop` | 非流式 agent loop |
| `run_tool_loop_stream` | 流式 agent loop |
| `run_agent(user_input)` | 单次任务，跑完就结束 |
| `chat()` | 多轮交互式对话 |

流式与非流式的返回结构不同，通过把工具调用统一成 `(id, name, arguments)` 三元组，让 `execute_tool_calls` 得以共用。

切换模式：

```python
chat()              # 非流式
chat(stream=True)   # 流式
```

## 踩过的坑

- **编辑器没有导入补全**：pyright 默认用 PATH 里的系统 python，不会自动探测项目里的 `.venv`。在 `pyproject.toml` 加 `[tool.pyright]` 的 `venvPath = "."` 和 `venv = ".venv"` 解决
- **tool 消息的 `content` 必须是字符串**：`eval()` 返回的是 `int`，要 `str()` 包一层
- **流式的 `index` 不可靠**：见 V4 要点

## 已知问题

- `tools.py` 用 `eval()` 执行模型给的表达式，等于任意代码执行，仅适用于本地学习，不能上线
- 没有上下文长度控制，多轮对话久了会超出 token 上限
- 没有请求重试和错误处理
