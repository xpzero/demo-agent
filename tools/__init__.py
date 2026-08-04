"""工具集合：每个工具一个模块，各自导出 SCHEMA（声明）与 run（实现）。"""

from . import calculate, get_weather, read_file, write_file

# 新增工具时只需在此登记模块，声明与实现都从模块里取，不会出现两处不同步
MODULES = (calculate, get_weather, read_file, write_file)

TOOLS = [module.SCHEMA for module in MODULES]

TOOL_HANDLERS = {module.SCHEMA["function"]["name"]: module.run for module in MODULES}


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
