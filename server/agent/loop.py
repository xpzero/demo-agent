import json
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor

from openai import APIConnectionError, APIStatusError, APITimeoutError

from tools import TOOLS, execute_tool
from tools.context import SessionContext

from .client import FALLBACK_MODEL, MODEL, client
from .metrics import ToolRecord, TurnRecorder, excerpt

MAX_RUN_SECONDS = 300
MAX_MODEL_ATTEMPTS = 3
MAX_LOGICAL_REQUESTS = 10
MODEL_REQUEST_SECONDS = 60


class RunStopped(Exception):
    """The caller requested a stop at a safe boundary."""


class ModelRequestTimeout(Exception):
    """Only this API attempt expired; the Run may still have time."""


class RecoveryFormatError(ValueError):
    """恢复模式下模型输出不符合 recovery_suggestion 协议（可安全重试）。"""


def _check_stop(should_stop: Callable[[], bool] | None) -> None:
    if should_stop is not None and should_stop():
        raise RunStopped()


def _retryable(error: Exception) -> bool:
    if isinstance(error, APIStatusError):
        return error.status_code == 429 or error.status_code >= 500
    return isinstance(error, (APIConnectionError, APITimeoutError))


def _check_deadline(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise TimeoutError("本轮回复超时，请重试")

# 工具 SCHEMA 是扁平格式，Chat Completions 请求需要多一层 function 外壳
CHAT_TOOLS = [
    {
        "type": "function",
        "function": {
            key: schema[key] for key in ("name", "description", "parameters")
        },
    }
    for schema in TOOLS
]

RECOVERY_TOOL = {
    "type": "function",
    "function": {
        "name": "recovery_suggestion",
        "description": "提出当前任务的结构化恢复建议；不会执行工具。",
        "parameters": {
            "type": "object",
            "properties": {
                "intent": {"type": "string", "enum": ["reply", "resume", "verify", "modify_goal", "clarify"]},
                "action": {"type": "string", "enum": ["reply", "execute", "verify", "modify_goal", "clarify"]},
                "task_id": {"type": "string", "description": "候选任务 id，必须用字符串"},
                "task_version": {"type": "integer"},
                "operation_refs": {"type": "array", "items": {"type": "object"},
                    "description": "必须完整列出该任务全部操作，每项只含 id（字符串）"},
                "question": {"type": "string"}, "goal": {"type": "string"},
                "operation_id": {"type": "string"}, "step_id": {"type": "string"},
                "tool": {"type": "string"}, "args": {"type": "object"},
            },
            "required": ["intent", "action", "task_id", "task_version", "operation_refs"],
        },
    },
}

# 恢复答复的呈现只依据后端核对过的事实：outcome 摘要 + 操作事实行，
# 模型自由文字在恢复模式下不透传，也不能成为业务结论来源。
_RECOVERY_OUTCOME_SUMMARIES = {
    "allow": "已核对当前任务事实。",
    "skip": "该操作已有可靠结果，无需重复执行。",
    "clarify": "需要你补充信息。",
    "blocked": "当前无法执行该建议。",
    "rejudge": "任务事实已变化，需要重新判断。",
    "invalid": "恢复建议未通过核对。",
    "adopted": "已更新任务目标。",
    "observed": "已向业务系统核实。",
}

_OPERATION_STATUS_LABELS = {
    "succeeded": "已成功",
    "failed": "已失败",
    "cancelled": "已确认取消",
    "processing": "仍在处理中（截至本次核对）",
    "maybe_submitted": "可能已提交，结果待核实（截至本次核对）",
    "unconfirmed": "结果待核实（截至本次核对）",
    "not_started": "尚未启动",
}


def _fact_line(fact: dict) -> str:
    status = fact.get("status")
    label = _OPERATION_STATUS_LABELS.get(status) if isinstance(status, str) else None
    line = f"- 操作 {fact.get('id')}（步骤 {fact.get('step_id')}）：{label or '状态未知'}"
    if fact.get("result"):
        line += f"：{fact['result']}"
    if fact.get("conflict"):
        line += "；存在矛盾的业务反馈，待业务方核实"
    return line


def _compose_recovery_answer(verdict: dict) -> str:
    """把核对后的结构化裁决组成用户可读的恢复答复。

    输入是后端校验器的 JSON 裁决（含已核对的 Operation 事实），
    输出是纯文本；不得把原始 JSON 或模型自由文字当作答复。
    呈现操作事实时标注本次核对时间：答复反映核对时点的已知事实，
    不是永久不变的结论（迟到业务反馈仍会更新最新事实）。
    """
    lines: list[str] = []
    message = verdict.get("message")
    if isinstance(message, str) and message.strip():
        lines.append(message.strip())
    outcome = verdict.get("outcome")
    showed_facts = False
    if outcome == "observed":
        operation_id = verdict.get("operation_id")
        status = verdict.get("status")
        if verdict.get("conflict"):
            lines.append(
                f"业务操作 {operation_id} 收到矛盾反馈，"
                f"保留已有结论（{verdict.get('confirmed_result')}），待业务方核实。"
            )
            showed_facts = True
        elif status in ("succeeded", "failed", "cancelled"):
            result = verdict.get("confirmed_result")
            lines.append(
                f"业务操作 {operation_id} {_OPERATION_STATUS_LABELS[status]}"
                + (f"：{result}" if result else "")
            )
            showed_facts = True
        else:
            label = _OPERATION_STATUS_LABELS.get(status, "结果待核实")
            lines.append(
                f"业务操作 {operation_id} {label}；"
                "不会自动重做该操作，也不会推进依赖它的步骤。"
            )
            showed_facts = True
    else:
        lines.append(_RECOVERY_OUTCOME_SUMMARIES.get(outcome, "恢复处理结束。"))
        facts = verdict.get("facts")
        if isinstance(facts, list) and facts:
            lines.extend(_fact_line(fact) for fact in facts if isinstance(fact, dict))
            showed_facts = True
        elif outcome in ("blocked", "rejudge", "invalid", "skip"):
            reason = verdict.get("reason")
            if isinstance(reason, str) and reason.strip():
                lines.append(reason)
    if showed_facts:
        lines.append(f"（以上为截至本次核对时间的已知事实，核对时间："
                     f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())}）")
    return "\n".join(lines)


# stream_events 产出的事件类型：
#   {"type": "text_delta", "text": str}                文本增量
#   {"type": "tool_call", "id": str, "name": str, "args": dict} 模型发起一次调用
#   {"type": "tool_result", "id": str, "content": str} 工具执行结果
#   {"type": "done", "content": str}                   模型给出最终回复
#   {"type": "max_turns"}                              触发轮次上限
#   {"type": "error", "message": str}                  请求或流处理失败
#   {"type": "stopped"}                              调用方停止本轮


def _merge_tool_call_delta(
    partial_tool_calls: dict[int, dict], tool_call_delta
) -> None:
    """把单个工具调用增量碎片并入对应 index 的累积记录（就地修改）。"""
    index = (
        tool_call_delta.index if tool_call_delta.index is not None else 0
    )
    partial_tool_call = partial_tool_calls.setdefault(
        index, {"id": "", "name": "", "arguments": ""}
    )
    if tool_call_delta.id:
        partial_tool_call["id"] = tool_call_delta.id
    if tool_call_delta.function is not None:
        if tool_call_delta.function.name:
            partial_tool_call["name"] = tool_call_delta.function.name
        if tool_call_delta.function.arguments:
            partial_tool_call["arguments"] += (
                tool_call_delta.function.arguments
            )


def _aggregate_stream(
    stream, text_parts: list[str], partial_tool_calls: dict[int, dict], deadline: float,
    current_turn=None, should_stop: Callable[[], bool] | None = None,
    request_deadline: float | None = None,
) -> Iterator[dict]:
    """消费模型的 chunk 流：文本增量向外透传，工具调用增量按 index 聚齐。

    结果就地写入 text_parts 和 partial_tool_calls，不产生返回值。
    current_turn 存在时（P0-2 埋点），从最后一个 chunk 提取 usage。
    """
    # 工具调用参数按 chunk 增量到达，必须按 index 聚齐后再解析
    for chunk in stream:
        _check_stop(should_stop)
        now = time.monotonic()
        if now >= deadline:
            raise TimeoutError("本轮回复超时，请重试")
        if request_deadline is not None and now >= request_deadline:
            raise ModelRequestTimeout("单次模型请求超时")
        if current_turn is not None:
            usage = _extract_usage(chunk)
            if usage:
                current_turn.usage = usage
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        if delta is None:
            continue
        # GLM 的深度思考走 reasoning_content，不属于用户可见文本，跳过
        if delta.content:
            text_parts.append(delta.content)
            yield {"type": "text_delta", "text": delta.content}
        for tool_call_delta in delta.tool_calls or []:
            _merge_tool_call_delta(partial_tool_calls, tool_call_delta)


def _parse_call_args(tool_calls: list[dict]) -> list[tuple[dict, dict]]:
    """把聚齐的调用记录解析成 (tool_call, args) 列表，参数必须是 JSON 对象。"""
    tool_calls_with_args = []
    for tool_call in tool_calls:
        args = json.loads(tool_call["arguments"] or "{}")
        if not isinstance(args, dict):
            raise TypeError(f"工具 {tool_call['name']} 的参数必须是 JSON 对象")
        tool_calls_with_args.append((tool_call, args))
    return tool_calls_with_args


def _build_assistant_message(text: str, tool_calls: list[dict]) -> dict:
    """构造携带本轮全部工具调用的 assistant 消息（调用请求，非结果）。"""
    return {
        "role": "assistant",
        "content": text or None,
        "tool_calls": [
            {
                "id": tool_call["id"],
                "type": "function",
                "function": {
                    "name": tool_call["name"],
                    "arguments": tool_call["arguments"],
                },
            }
            for tool_call in tool_calls
        ],
    }


# P2-5：只读工具并发执行；副作用工具（write_file/calculate）串行殿后。
# 新增只读工具时在此登记；不在名单内的工具一律按副作用处理（保守默认）。
READONLY_TOOL_NAMES = frozenset(
    {"read_file", "web_search", "fetch_url", "get_weather", "parse_attached_document"}
)
TOOL_CONCURRENCY = 4


def _run_tool_calls(
    tool_calls_with_args: list[tuple[dict, dict]],
    items: list,
    deadline: float,
    context: SessionContext | None,
    current_turn=None,
    should_stop: Callable[[], bool] | None = None,
    on_tool_attempt: Callable[[str, str, dict], Callable[[str, bool], None]] | None = None,
    on_recovery_suggestion: Callable[[dict], str] | None = None,
) -> Iterator[dict]:
    """同轮工具分两段执行，事件按模型调用顺序同序产出。

    只读工具（READONLY_TOOL_NAMES）的执行体丢进线程池并发跑；
    副作用工具等并发段全部完成后在主线程串行执行，顺序与模型
    调用顺序一致。恢复账本（on_tool_attempt）与 ToolRecord 埋点
    始终在主线程按调用顺序登记，不引入并发写。current_turn 存在
    时（P0-2 埋点），每个工具的名/参数副本/结果副本/耗时/成败
    记入该次模型请求的观测记录；为 None 时零开销。
    """
    _check_deadline(deadline)
    _check_stop(should_stop)
    # 第 1 步：按调用顺序亮牌
    for tool_call, args in tool_calls_with_args:
        yield {
            "type": "tool_call",
            "id": tool_call["id"],
            "name": tool_call["name"],
            "args": args,
        }

    def _execute(tool_call: dict, args: dict):
        recovery = (tool_call["name"] == "recovery_suggestion"
                    and on_recovery_suggestion is not None)
        tool_started = time.monotonic()
        if recovery:
            output, ok = on_recovery_suggestion(args), True
        else:
            output, ok = execute_tool(tool_call["name"], args, context)
        # 耗时在执行线程内计算真实执行时长；批内等待不计入
        return output, ok, (time.monotonic() - tool_started) * 1000

    def _attempt(tool_call: dict, args: dict):
        recovery = tool_call["name"] == "recovery_suggestion" and on_recovery_suggestion is not None
        if on_tool_attempt is None or recovery:
            return None
        return on_tool_attempt(tool_call["id"], tool_call["name"], args)

    # 第 2 步：主线程按调用序登记并发段的账（快，无耗时 I/O）
    readonly_indexes = [
        i for i, (call, _) in enumerate(tool_calls_with_args)
        if call["name"] in READONLY_TOOL_NAMES
    ]
    finishers = {}
    for i in readonly_indexes:
        tool_call, args = tool_calls_with_args[i]
        finishers[i] = _attempt(tool_call, args)

    # 第 3 步：只读工具执行体并发；耗时只发生在这里
    results: dict[int, tuple[str, bool, float]] = {}
    if readonly_indexes:
        with ThreadPoolExecutor(max_workers=TOOL_CONCURRENCY) as pool:
            futures = {
                i: pool.submit(_execute, tool_calls_with_args[i][0], tool_calls_with_args[i][1])
                for i in readonly_indexes
            }
            for i, future in futures.items():
                try:
                    results[i] = future.result()
                except BaseException as error:
                    finishers[i](f"{type(error).__name__}: {error}", False)
                    raise
        _check_deadline(deadline)
        _check_stop(should_stop)

    # 第 4 步：副作用/recovery 工具主线程串行殿后（登记贴着执行）
    for i, (tool_call, args) in enumerate(tool_calls_with_args):
        if i in readonly_indexes:
            continue
        _check_deadline(deadline)
        _check_stop(should_stop)
        finishers[i] = _attempt(tool_call, args)
        try:
            results[i] = _execute(tool_call, args)
        except BaseException as error:
            finishers[i](f"{type(error).__name__}: {error}", False)
            raise

    # 第 5 步：按原调用顺序交卷——销账、埋点、回填、发事件
    for i, (tool_call, args) in enumerate(tool_calls_with_args):
        output, ok, tool_elapsed_ms = results[i]
        finisher = finishers.get(i)
        if finisher is not None:
            finisher(output, ok)
        items.append(
            {"role": "tool", "tool_call_id": tool_call["id"], "content": output}
        )
        if current_turn is not None:
            current_turn.tools.append(
                ToolRecord(
                    name=tool_call["name"],
                    args_excerpt=excerpt(args),
                    result_excerpt=excerpt(output),
                    duration_ms=tool_elapsed_ms,
                    ok=ok,
                )
            )
        yield {
            "type": "tool_result",
            "id": tool_call["id"],
            "content": output,
            "elapsed": tool_elapsed_ms,
        }


def _extract_usage(chunk) -> dict | None:
    """从流式 chunk 提取 usage（OpenAI 兼容接口在最后一个 chunk 携带）。

    接口不返回时保持 None，由调用方按文本估算并标 estimated。
    """
    usage = getattr(chunk, "usage", None)
    if not usage:
        return None
    data = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = getattr(usage, key, None)
        if isinstance(value, int):
            data[key] = value
    return data or None


def stream_events(
    items: list,
    max_turns: int = 10,
    context: SessionContext | None = None,
    recorder: TurnRecorder | None = None,
    should_stop: Callable[[], bool] | None = None,
    max_attempts: int = MAX_MODEL_ATTEMPTS,
    fallback_model: str | None = FALLBACK_MODEL,
    deadline_at: float | None = None,
    on_model_attempt: Callable[[bool], None] | None = None,
    on_tool_attempt: Callable[[str, str, dict], Callable[[str, bool], None]] | None = None,
    on_recovery_suggestion: Callable[[dict], str] | None = None,
    recovery_prompt: str | None = None,
) -> Iterator[dict]:
    """agent loop 的核心：流式请求模型、执行工具，把过程产出为结构化事件。

    items 是 Chat Completions 的 messages 列表，会被原地追加。
    context 携带本轮会话信息，供 parse_attached_document 这类
    需要定位会话附件的工具使用，与事件流本身无关。
    recorder 携带内存账本（P0-2 埋点）：逐次模型请求的耗时与 usage、
    每个工具的名/耗时/成败记入其中，落库由调用方决定——loop 不碰库。
    should_stop 在请求、流和工具边界检查；deadline_at 是 Unix 时间戳，
    未传时限时 300 秒。max_attempts 限制单个逻辑请求的实际尝试次数；
    fallback_model 只用于最后一次尝试。
    不做任何打印——HTTP 服务是事件流的消费方，这里只负责「发生了什么」，
    怎么呈现由调用方决定。
    """
    remaining = MAX_RUN_SECONDS if deadline_at is None else max(0, deadline_at - time.time())
    deadline = time.monotonic() + remaining
    max_attempts = min(MAX_MODEL_ATTEMPTS, max(1, max_attempts))
    max_turns = min(MAX_LOGICAL_REQUESTS, max(0, max_turns))
    logical_requests = 0
    recovery_retries = 0
    try:
        while logical_requests < max_turns:
            logical_requests += 1
            failures = 0
            while True:
                _check_stop(should_stop)
                _check_deadline(deadline)
                if on_model_attempt is not None:
                    on_model_attempt(failures == 0)
                current_turn = recorder.start_turn() if recorder is not None else None
                if current_turn is not None:
                    current_turn.items_snapshot = [
                        {**item} if isinstance(item, dict) else item
                        for item in items
                    ]
                text_parts: list[str] = []
                partial_tool_calls: dict[int, dict] = {}
                emitted = False
                started = time.monotonic()
                stream = None
                try:
                    model = (
                        fallback_model
                        if fallback_model and failures > 0 and failures == max_attempts - 1
                        else MODEL
                    )
                    stream = client.chat.completions.create(
                        model=model,
                        messages=([{"role": "system", "content": recovery_prompt}] + items
                                  if recovery_prompt is not None else items),
                        tools=CHAT_TOOLS + [RECOVERY_TOOL] if recovery_prompt is not None else CHAT_TOOLS,
                        stream=True,
                        stream_options={"include_usage": True},
                        timeout=min(MODEL_REQUEST_SECONDS, max(0, deadline - started)),
                    )
                    for event in _aggregate_stream(
                        stream, text_parts, partial_tool_calls, deadline,
                        current_turn=current_turn, should_stop=should_stop,
                        request_deadline=started + MODEL_REQUEST_SECONDS,
                    ):
                        if recovery_prompt is None:
                            emitted = True
                            yield event
                    if not partial_tool_calls:
                        now = time.monotonic()
                        if now >= deadline:
                            raise TimeoutError("本轮回复超时，请重试")
                        if now >= started + MODEL_REQUEST_SECONDS:
                            raise ModelRequestTimeout("单次模型请求超时")
                except Exception as error:
                    if current_turn is not None:
                        current_turn.ok = False
                    if (emitted or text_parts or partial_tool_calls or
                            not (_retryable(error) or isinstance(error, ModelRequestTimeout)) or
                            failures + 1 >= max_attempts):
                        raise
                    failures += 1
                    _check_deadline(deadline)
                    _check_stop(should_stop)
                    delay = min(0.25 * 2 ** (failures - 1), 1.0,
                                max(0, deadline - time.monotonic()))
                    if should_stop is None:
                        time.sleep(delay)
                    else:
                        until = time.monotonic() + delay
                        while time.monotonic() < until:
                            _check_stop(should_stop)
                            _check_deadline(deadline)
                            remaining = until - time.monotonic()
                            if remaining <= 0:
                                break
                            time.sleep(min(0.05, remaining))
                    continue
                finally:
                    if stream is not None and hasattr(stream, "close"):
                        try:
                            stream.close()
                        except Exception:
                            pass  # Preserve the request's original failure.
                    if current_turn is not None:
                        current_turn.duration_ms = (time.monotonic() - started) * 1000
                break

            _check_stop(should_stop)
            if recovery_prompt is not None:
                _check_deadline(deadline)
                if on_recovery_suggestion is None:
                    raise RecoveryFormatError("恢复判断缺少校验器")
                tool_calls = [partial_tool_calls[index] for index in sorted(partial_tool_calls)]
                if len(tool_calls) != 1 or tool_calls[0]["name"] != "recovery_suggestion":
                    if recovery_retries < 1:
                        # One corrective retry: nudge the model to use the tool.
                        recovery_retries += 1
                        items.append({"role": "assistant", "content": "".join(text_parts) or "(空回复)"})
                        items.append({"role": "user", "content":
                                      "上一条回复没有调用 recovery_suggestion 工具。"
                                      "恢复判断必须且只能提交一个 recovery_suggestion 工具调用，不要输出普通文本。"})
                        text_parts.clear()
                        partial_tool_calls.clear()
                        continue
                    raise RecoveryFormatError("恢复判断需要唯一的 recovery_suggestion")
                suggestion = _parse_call_args(tool_calls)[0][1]
                verdict = on_recovery_suggestion(suggestion)
                _check_stop(should_stop)
                _check_deadline(deadline)
                if not isinstance(verdict, str):
                    raise TypeError("恢复校验结果必须是文本")
                try:
                    parsed_verdict = json.loads(verdict)
                    outcome = parsed_verdict["outcome"]
                except (ValueError, TypeError, KeyError) as error:
                    raise RecoveryFormatError("恢复校验结果无效") from error
                if outcome not in {"allow", "skip", "clarify", "blocked", "rejudge", "invalid", "observed", "adopted"}:
                    raise RecoveryFormatError("恢复校验结果无效")
                # done.content 只呈现核对后的事实：由后端把结构化裁决
                # 组成用户可读文本，原始 JSON 仅保留在 tool_result 中。
                answer = _compose_recovery_answer(parsed_verdict)
                if current_turn is not None:
                    current_turn.reply_text = answer
                yield {"type": "tool_call", "id": tool_calls[0]["id"],
                       "name": "recovery_suggestion", "args": suggestion}
                _check_stop(should_stop)
                yield {"type": "tool_result", "id": tool_calls[0]["id"],
                       "content": verdict, "elapsed": 0.0}
                _check_stop(should_stop)
                if outcome == "invalid":
                    reason = str(parsed_verdict.get("reason") or "")
                    if recovery_retries < 1:
                        # Strict citation rarely succeeds on the first try;
                        # give the model one corrective retry with the reason.
                        recovery_retries += 1
                        items.append({"role": "assistant", "content": answer or "(空回复)"})
                        items.append({"role": "user", "content":
                                      "上一个 recovery_suggestion 未通过校验：" + reason +
                                      "。请严格按照任务事实重新提交一个 recovery_suggestion 工具调用，"
                                      "operation_refs 必须完整引用该任务全部操作及其准确状态（succeeded/failed/cancelled/"
                                      "processing/maybe_submitted/unconfirmed），task_id 用字符串。"})
                        continue
                    raise RecoveryFormatError("恢复建议未通过校验")
                items.append({"role": "assistant", "content": answer})
                yield {"type": "done", "content": answer}
                return
            if not partial_tool_calls:
                reply = "".join(text_parts)
                if current_turn is not None:
                    current_turn.reply_text = reply
                # 最终回复也要写回上下文，否则模型下一轮看不到自己说过什么
                if reply:
                    items.append({"role": "assistant", "content": reply})
                yield {"type": "done", "content": reply}
                return

            tool_calls = [
                partial_tool_calls[index]
                for index in sorted(partial_tool_calls)
            ]
            # 先确认所有参数都能解析，再追加 assistant 消息，
            # 避免留下只有调用、没有结果的半截历史。
            tool_calls_with_args = _parse_call_args(tool_calls)
            items.append(
                _build_assistant_message("".join(text_parts), tool_calls)
            )
            assistant_index = len(items) - 1
            try:
                yield from _run_tool_calls(
                    tool_calls_with_args, items, deadline, context, current_turn,
                    should_stop=should_stop, on_tool_attempt=on_tool_attempt,
                    on_recovery_suggestion=on_recovery_suggestion,
                )
            except BaseException:
                completed = len(items) - assistant_index - 1
                if completed:
                    items[assistant_index]["tool_calls"] = items[assistant_index]["tool_calls"][:completed]
                else:
                    items.pop()
                raise

        yield {"type": "max_turns"}
    except RunStopped:
        yield {"type": "stopped"}
    except TimeoutError as error:
        yield {"type": "error", "code": "timed_out", "message": f"TimeoutError: {error}"}
    except Exception as error:
        yield {"type": "error", "message": f"{type(error).__name__}: {error}"}
