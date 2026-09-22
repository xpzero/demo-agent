"""工具集合：按运行配置生成模型声明与执行函数。"""

import os

from . import calculate, files, get_weather, pdf, web
from .context import SessionContext

# 新增工具时在对应子包的 MODULES 里登记；工具名与实现都从模块的 SCHEMA 取，不会两处不同步
MODULES = (calculate, get_weather, pdf, *files.MODULES, *web.MODULES)

# 公开模式使用明确的允许名单。高风险教学工具不能只从提示词隐藏，
# 还必须同时从模型 schema 与执行分发中移除。
PUBLIC_TOOL_NAMES = frozenset({"get_weather", "web_search"})
TOOL_PROFILE = os.getenv("TOOL_PROFILE", "local").strip().lower()


def build_registry(profile: str) -> tuple[list[dict], dict]:
    """按配置返回工具 schema 与处理器；未知配置直接拒绝启动。"""
    if profile == "local":
        modules = MODULES
    elif profile == "public":
        modules = tuple(
            module
            for module in MODULES
            if module.SCHEMA["name"] in PUBLIC_TOOL_NAMES
        )
    else:
        raise ValueError(f"未知 TOOL_PROFILE：{profile}")

    tools = [module.SCHEMA for module in modules]
    handlers = {module.SCHEMA["name"]: module.run for module in modules}
    return tools, handlers


TOOLS, TOOL_HANDLERS = build_registry(TOOL_PROFILE)


def execute_tool(name: str, args: dict, context: SessionContext | None = None) -> str:
    """按名字分发到具体工具，并把异常统一转成文本结果。

    args 由模型生成，属于不可信输入：字段缺失、路径越界、表达式非法都有可能。
    这里兜住所有异常并把错误信息回传给模型，它就有机会自行纠正后重试，
    而不是让整个 agent 循环直接崩掉。context 携带本轮会话信息，
    供需要定位会话附件的工具使用；无会话上下文的调用方不传即可。
    """
    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        return f"未知工具：{name}"

    try:
        return handler(args, context)
    except Exception as e:
        return f"{name} 执行出错：{e}"
