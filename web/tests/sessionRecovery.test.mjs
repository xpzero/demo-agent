import assert from "node:assert/strict";
import { test } from "node:test";
import { getSessionStatus, stopSession, streamChat } from "../src/adapter/index.ts";
import { HttpError } from "../src/adapter/transport.ts";
import { recoverSession, recoveryPollInterval } from "../src/chat/sessionRecovery.ts";

test("恢复轮询默认 5 秒，可配置且限制在 2–60 秒", () => {
  for (const value of [undefined, "", "invalid", "0", "-1", "Infinity"]) {
    assert.equal(recoveryPollInterval(value), 5000);
  }
  assert.equal(recoveryPollInterval("10000"), 10000);
  assert.equal(recoveryPollInterval("100"), 2000);
  assert.equal(recoveryPollInterval("90000"), 60000);
});

const history = (canSend = true) => ({
  session: { current_message_id: 42, summary_upto_message_id: 20 },
  messages: [{ id: 42, role: "assistant", content: "恢复的回复" }],
  can_send_message: canSend,
});
const json = (data, status = 200) => new Response(JSON.stringify(data), { status });

function mockFetch(t, responses) {
  const calls = [];
  t.mock.method(globalThis, "fetch", async (url, options) => {
    calls.push({ url, options });
    const next = responses.shift();
    if (next instanceof Error) {
      throw next;
    }
    assert.ok(next, "没有重复提交或多余请求");
    return next;
  });
  return calls;
}

test("处理期间只查询状态，完成后恢复历史与父节点", async (t) => {
  const calls = mockFetch(t, [
    json({ processing: true, can_send_message: false }),
    json({ processing: false, can_send_message: true }),
    json(history()),
  ]);
  const signal = new AbortController().signal;
  assert.deepEqual(await recoverSession("session/id", signal), { ready: false });
  const recovered = await recoverSession("session/id", signal);
  assert.equal(recovered.ready, true);
  assert.equal(recovered.history.session.current_message_id, 42);
  assert.equal(recovered.history.messages[0].content, "恢复的回复");
  assert.equal(calls.length, 3);
  assert.match(calls[0].url, /session%2Fid\/status$/);
  assert.equal(calls[0].options.signal, signal);
  assert.ok(calls.every(({ options }) => !options.method));
});

test("状态允许发送但历史发现活动请求时继续等待", async (t) => {
  mockFetch(t, [json({ processing: false, can_send_message: true }), json(history(false))]);
  assert.deepEqual(await recoverSession("id", new AbortController().signal), { ready: false });
});

test("恢复网络失败不会返回 ready，下次检查可以恢复", async (t) => {
  mockFetch(t, [new Error("offline"), json({ processing: false, can_send_message: true }), json(history())]);
  const signal = new AbortController().signal;
  await assert.rejects(recoverSession("id", signal), /offline/);
  assert.equal((await recoverSession("id", signal)).ready, true);
});

test("首帧前断流且会话未创建时允许重新发送", async (t) => {
  mockFetch(t, [json({}, 404), json({}, 404)]);
  assert.deepEqual(await recoverSession("id", new AbortController().signal), { ready: true, history: null });
});

test("状态和停止接口传递 signal，停止使用 POST 并保留三种结果", async (t) => {
  const calls = mockFetch(t, [
    json({ processing: true, can_send_message: false }),
    ...["processing", "ended", "no_active_request"].map((result) => json({ result })),
  ]);
  const signal = new AbortController().signal;
  assert.equal((await getSessionStatus("id", signal)).processing, true);
  for (const result of ["processing", "ended", "no_active_request"]) {
    assert.deepEqual(await stopSession("id", signal), { result });
  }
  for (const call of calls.slice(1)) {
    assert.equal(call.options.method, "POST");
    assert.equal(call.options.signal, signal);
    assert.match(call.url, /\/stop$/);
  }
});

test("409 拒绝聊天流，保留 HTTP 状态、session_busy 错误码与后端提示", async (t) => {
  mockFetch(t, [json({ detail: { code: "session_busy", message: "当前会话正在处理" } }, 409)]);
  await assert.rejects(streamChat({ session_id: "id", parent_message_id: null, message: "保留草稿", ref_file_ids: ["pdf"] }),
    (error) => error instanceof HttpError && error.status === 409 && error.code === "session_busy" && error.message === "当前会话正在处理");
});

test("错误响应无 code 字段时 HttpError.code 为 null 而非 undefined", async (t) => {
  mockFetch(t, [json({ detail: { message: "无码错误" } }, 500)]);
  await assert.rejects(getSessionStatus("id"), (error) => error instanceof HttpError && error.code === null);
});

test("停止失败提供可见错误", async (t) => {
  mockFetch(t, [json({ detail: "停止失败" }, 503)]);
  await assert.rejects(stopSession("id"), /停止失败/);
});

test("会话切换可取消恢复请求", async (t) => {
  t.mock.method(globalThis, "fetch", async (_url, { signal }) => {
    signal.throwIfAborted();
  });
  const controller = new AbortController();
  controller.abort();
  await assert.rejects(recoverSession("old", controller.signal), { name: "AbortError" });
});
