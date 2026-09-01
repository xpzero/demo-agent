"""工具集合：模型声明、执行函数与权限请求描述。"""

from services.permission import PermissionRequest

from . import calculate, files, get_weather, web

# 新增工具时在对应子包的 MODULES 里登记；工具名与实现都从模块的 SCHEMA 取，不会两处不同步
MODULES = (calculate, get_weather, *files.MODULES, *web.MODULES)

for module in MODULES:
    if not hasattr(module, "permission_requests"):
        raise ValueError(f"{module.SCHEMA['name']} 没有声明权限请求")

TOOLS = [module.SCHEMA for module in MODULES]

TOOL_HANDLERS = {module.SCHEMA["name"]: module.run for module in MODULES}
TOOL_PERMISSION_BUILDERS = {
    module.SCHEMA["name"]: module.permission_requests for module in MODULES
}
TOOL_PREVIEWS = {
    module.SCHEMA["name"]: module.preview
    for module in MODULES
    if hasattr(module, "preview")
}
TOOL_APPROVED_HANDLERS = {
    module.SCHEMA["name"]: module.run_approved
    for module in MODULES
    if hasattr(module, "run_approved")
}


def describe_tool_permissions(name: str, args: dict) -> tuple[PermissionRequest, ...]:
    builder = TOOL_PERMISSION_BUILDERS.get(name)
    if builder is None:
        raise ValueError(f"未知工具：{name}")

    requests = tuple(builder(args))
    if not requests:
        raise ValueError(f"工具 {name} 没有生成权限请求")
    if any(not isinstance(request, PermissionRequest) for request in requests):
        raise TypeError(f"工具 {name} 生成了无效权限请求")
    return requests


def preview_tool(name: str, args: dict) -> dict | None:
    preview = TOOL_PREVIEWS.get(name)
    return preview(args) if preview else None


def execute_approved_tool(name: str, args: dict, guard: dict | None) -> str:
    handler = TOOL_APPROVED_HANDLERS.get(name)
    if handler is not None:
        return handler(args, guard)

    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        raise ValueError(f"未知工具：{name}")
    return handler(args)


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
