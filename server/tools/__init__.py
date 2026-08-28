"""工具集合：一个工具一个文件，按领域分子包，各模块导出 SCHEMA（声明）与 run（实现）。"""

from . import calculate, files, get_weather, web

# 新增工具时在对应子包的 MODULES 里登记；工具名与实现都从模块的 SCHEMA 取，不会两处不同步
MODULES = (calculate, get_weather, *files.MODULES, *web.MODULES)

TOOLS = [module.SCHEMA for module in MODULES]

TOOL_HANDLERS = {module.SCHEMA["name"]: module.run for module in MODULES}


def execute_tool(name: str, args: dict) -> str:
    """按名字分发到具体工具，并把异常统一转成文本结果。

    args 由模型生成，属于不可信输入：字段缺失、路径越界、表达式非法都有可能。
    这里兜住所有异常并把错误信息回传给模型，它就有机会自行纠正后重试，
    而不是让整个 agent 循环直接崩掉。
    """
    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        return f"未知工具：{name}"

    try:
        return handler(args)
    except Exception as e:
        return f"{name} 执行出错：{e}"
