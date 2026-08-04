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

只需给请求加 `stream=True`，但处理方式变化不小。

#### 流式的本质

非流式返回一个**完整对象**；流式返回一串 **chunk**，每个 chunk 只带「这一小步新增了什么」，即 `delta`。所以取值是 `chunk.choices[0].delta`，不是 `.message`。

`delta` 有三种形态：只有文本、只有工具调用片段、什么都没有（收尾那个带 `finish_reason` 的 chunk）。因此两个分支都得写成「有才处理」。

实际抓到的序列（本项目所用网关）：

```
chunk[0]  tool_call(index=0, id='call_CxY3...', name='get_weather', args='{"city":"北京"}')
chunk[1]  tool_call(index=0, id='call_ahWJ...', name='calculate',   args='{"expression":"(38-12)*3"}')
chunk[2]  finish='stop'  (空)
```

#### 文本：边打印边留副本

```python
if delta.content:
    if not content:
        print("Agent: ", end="", flush=True)   # 首片段才打前缀
    print(delta.content, end="", flush=True)   # 实时输出
    content += delta.content                   # 同时攒完整文本
```

打印是给人看的，`content` 是给模型看的——结束后要把完整文本回填 `messages`，所以两份都要留。`end=""` 去掉自动换行，`flush=True` 强制立刻输出，否则会被缓冲攒住、流式效果消失。

#### 工具调用：归拢碎片

一个工具调用会被拆到多个 chunk。标准 OpenAI 的形态是：

```
chunk0  id='call_A'  name='get_weather'  args=''
chunk1  id=None      name=None           args='{"ci'
chunk2  id=None      name=None           args='ty":"北'
chunk3  id=None      name=None           args='京"}'
chunk4  id='call_B'  name='calculate'    args=''      ← 换调用了
```

只有**首个片段带 `id` 和 `name`**，后续片段只有 `arguments` 的一截。所以要维护一个「进行中的调用」列表：

```python
for tc in delta.tool_calls or []:
    # ① 没见过的 id → 开一条新记录
    if not calls or (tc.id and all(c["id"] != tc.id for c in calls)):
        calls.append({"id": tc.id or "", "name": "", "arguments": ""})

    # ② 找到这个片段该归到哪条记录
    slot = next((c for c in calls if tc.id and c["id"] == tc.id), calls[-1])

    # ③ 原地写入
    if tc.function and tc.function.name:
        slot["name"] = tc.function.name              # 覆盖，name 不会被切碎
    if tc.function and tc.function.arguments:
        slot["arguments"] += tc.function.arguments   # 累加，参数是碎片
```

按上面的标准形态推演：

| chunk | 动作 | `calls` 状态 |
|---|---|---|
| 0 | `call_A` 没见过 → 新建 | `[{A, get_weather, ""}]` |
| 1 | 无 id → 落到 `calls[-1]` | `[{A, get_weather, '{"ci'}]` |
| 2 | 同上 | `[{A, ..., '{"city":"北'}]` |
| 3 | 同上 | `[{A, ..., '{"city":"北京"}'}]` ← 完整 |
| 4 | `call_B` 没见过 → 新建 | `[{A...}, {B, calculate, ""}]` |

几个易错点：

- **`arguments` 必须 `+=` 不能 `=`**：碎片被覆盖后只剩最后一截，`json.loads` 必然报错。而 `name` 相反，首片段一次给全，直接赋值
- **不存在「调用结束」信号**，也不需要。`slot` 是字典的**引用**，写 `slot` 就是写进 `calls`，属于原地增量更新，循环退出时每条自然都是完整的
- **`or []`**：纯文本 chunk 里 `delta.tool_calls` 是 `None`，直接迭代会 `TypeError`
- ⚠️ 标准实现中每个工具调用有递增的 `index`，但**实测本项目使用的网关对多个调用一律返回 `index=0`**。按 index 归拢会把两串参数拼成 `{"city":"北京"}{"expression":"..."}` → 报 `Extra data`。改成按 `id` 归拢，不依赖 `index`，也不怕片段交错送达
- `id` 是「每次调用」唯一而非「每个工具」唯一，所以同一个工具被连续调用两次会得到两条独立记录，不会被误合并

> Chat Completions 的流式实际是顺序的（一个调用的片段送完才开始下一个），只跟 `calls[-1]` 比也能跑通。但协议未明文保证不交错，且已见到该网关不遵守 `index` 惯例，按 `id` 查属于零成本的防御。

#### 收尾：重建 assistant 消息

流式没有现成的 message 对象，必须按非流式的结构自己拼一条塞回上下文：

```python
messages.append({
    "role": "assistant",
    "content": content or None,
    "tool_calls": [{"id": ..., "type": "function", "function": {...}} for slot in calls],
})
```

**这一步不能省**。下一轮请求时，模型靠上下文里这条消息才知道自己发起过哪些调用；紧随其后的 `role="tool"` 消息也靠 `tool_call_id` 与它对应。缺了它 API 会因 tool 消息找不到归属而报错。

最后 `if not calls: return`——没有工具调用说明模型给的是最终回复，本轮结束。

### V5 · 文件读写工具

目标：让 Agent 能真正操作文件，同时把工具体系整理成可扩展的结构。

#### 工具分发：字典映射 + 统一兜底

工具变多后 `if/elif` 就不合适了，改成字典分发，`execute_tool` 只负责查表、调用、兜异常：

```python
def execute_tool(name: str, args: dict) -> str:
    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        return f"未知工具：{name}"

    try:
        return handler(args)
    except Exception as e:
        return f"{name} 执行出错：{e}"
```

这里捕获所有异常是**刻意的**：`args` 由模型生成，字段缺失、路径越界、表达式非法都可能发生。把错误转成文本回传，模型下一轮就能看到原因并自行纠正；若直接抛出，整个 agent 循环会崩。

```
缺字段:      calculate 执行出错：'expression'
非法表达式:  calculate 执行出错：invalid syntax (<string>, line 1)
未知工具:    未知工具：foo
```

#### 目录拆分：一个工具一个文件

```
tools/
├── __init__.py      # 登记模块，汇总 TOOLS / TOOL_HANDLERS
├── paths.py         # 共享的路径校验
├── calculate.py
├── get_weather.py
├── read_file.py
└── write_file.py
```

每个工具文件导出两样东西——`SCHEMA`（给模型的声明）和 `run(args)`（实现）。声明与实现放在同一个文件，就不会出现「加了实现忘了加声明」。

`__init__.py` 里工具名直接从 `SCHEMA` 提取，不再手写第二遍：

```python
MODULES = (calculate, get_weather, read_file, write_file)

TOOLS = [module.SCHEMA for module in MODULES]
TOOL_HANDLERS = {module.SCHEMA["function"]["name"]: module.run for module in MODULES}
```

#### 路径安全：必须限定边界

`path` 是模型生成的不可信输入。不加限制，它就能读 `~/.ssh/id_rsa` 或覆盖任意文件。`tools/paths.py` 做两层校验：

```python
ROOT = Path(__file__).resolve().parent.parent   # 根目录固定为项目目录
BLOCKED = {".env", ".git"}

def resolve(path: str) -> Path:
    target = (ROOT / path).resolve()            # resolve 会展开 .. 与符号链接

    if not target.is_relative_to(ROOT):
        raise ValueError(f"路径越界，只能访问项目目录内的文件：{path}")

    relative = target.relative_to(ROOT)
    if relative.parts and relative.parts[0] in BLOCKED:
        raise ValueError(f"该路径禁止访问：{path}")

    return target
```

- **先 `resolve()` 再判断**：`..` 和符号链接都会被展开成真实路径，绕不过去
- **屏蔽 `.env`**：里面是 API key。一旦被 `read_file` 读出来，就会作为 tool 结果进入 messages，随下一次请求发给服务端
- **屏蔽 `.git`**：避免仓库元数据被改写

效果：

```
越界:      read_file 执行出错：路径越界，只能访问项目目录内的文件：../../etc/hosts
绝对路径:  read_file 执行出错：路径越界，只能访问项目目录内的文件：/etc/hosts
黑名单:    read_file 执行出错：该路径禁止访问：.env
```

#### 怎么用

不需要写任何调用代码，直接用自然语言说，模型自己决定调哪个工具：

```
$ uv run main.py
Agent 已启动，输入 quit 退出
你: 在 notes 目录下建一个 hello.md，内容写一句项目介绍
  [工具调用] write_file({'content': '这是一个用于演示和实践的项目……', 'path': 'notes/hello.md'})
  [返回结果] 已写入 notes/hello.md（37 字符）
Agent: 已在 `notes/hello.md` 中写入项目介绍。

你: 读一下 tools/calculate.py，告诉我它导出了什么
  [工具调用] read_file({'path': 'tools/calculate.py'})
Agent: 导出了 SCHEMA（描述 calculate 工具及其 expression 参数）和 run(args)……
```

`write_file` 会自动创建缺失的父目录，文件已存在则**直接覆盖**，没有确认环节。

#### 新增一个工具

1. 在 `tools/` 下建文件，导出 `SCHEMA` 和 `run(args)`
2. 在 `tools/__init__.py` 的 `MODULES` 里加上这个模块

没有第三步——`TOOLS` 和 `TOOL_HANDLERS` 都是自动派生的。

## 当前结构

```
demo-agent/
├── main.py          # 入口，只负责启动
├── agent.py         # 对话流程与 agent loop
├── tools/           # 工具集合，一个工具一个文件
│   ├── __init__.py  # 登记模块，汇总 TOOLS / TOOL_HANDLERS
│   ├── paths.py     # 文件类工具共享的路径校验
│   ├── calculate.py
│   ├── get_weather.py
│   ├── read_file.py
│   └── write_file.py
└── .env             # API key 与 base url
```

现有工具：

| 工具 | 作用 |
|---|---|
| `calculate` | 计算数学表达式 |
| `get_weather` | 查天气（写死的假数据） |
| `read_file` | 读项目内文件 |
| `write_file` | 写项目内文件，自动建父目录，已存在则覆盖 |

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

- `tools/calculate.py` 用 `eval()` 执行模型给的表达式，等于任意代码执行，仅适用于本地学习，不能上线
- `write_file` 直接覆盖已有文件，没有确认或备份环节，改坏了只能靠 git 找回
- 没有上下文长度控制，多轮对话久了会超出 token 上限
- 没有请求重试和错误处理
