import assert from "node:assert/strict";
import { test } from "node:test";
import { needsResume, resumeSnapshot, recoverSession } from "../src/chat/sessionRecovery.ts";
import { applyChatEvent } from "../src/chat/messageUpdates.ts";
import { calibrateHistory } from "../src/chat/messageHistory.ts";
import { readSse } from "../src/adapter/transport.ts";

const user = { id: 1, parent_id: null, role: "user", status: "finished", content: "问题", created_at: 1, files: [] };
const assistant = { id: 2, parent_id: 1, role: "assistant", status: "incomplete", content: "前文", created_at: 2, files: [] };
const history = { session: { id: "session", current_message_id: 2, summary_upto_message_id: null }, messages: [user, assistant], can_send_message: false };
const stream = (text) => new ReadableStream({ start(controller) { controller.enqueue(new TextEncoder().encode(text)); controller.close(); } });

test("恢复触发覆盖等待首片段、未完成文字和业务收尾", () => {
  assert.equal(needsResume(history), true);
  assert.equal(needsResume({ ...history, can_send_message: true }), true);
  assert.equal(needsResume({ ...history, messages: [user] }), true);
  assert.equal(needsResume({ ...history, can_send_message: false, messages: [user, { ...assistant, status: "finished" }] }), true);
  assert.equal(needsResume({ ...history, can_send_message: true, messages: [user, { ...assistant, status: "finished" }] }), false);
});

test("快照清空当前助手回放，用户不重复且文本工具顺序在校准后保留", () => {
  const { messages, ids } = resumeSnapshot({ type: "resume_snapshot", history, user_message_id: 1, replay: true });
  assert.equal(messages.length, 2);
  assert.deepEqual(messages[1].events, []);
  let current = messages;
  const events = [{ type: "text_delta", text: "前文" }, { type: "tool_call", id: "call", name: "get_weather", args: {} }, { type: "tool_result", id: "call", content: "晴" }, { type: "text_delta", text: "后文" }];
  for (const event of events) current = applyChatEvent(current, event, ids, 2);
  const calibrated = calibrateHistory(current, [user, { ...assistant, content: "前文后文", status: "finished" }], ids);
  assert.deepEqual(calibrated[1].events, events);
  assert.equal(calibrated[1].incomplete, false);
});

test("事实快照保留未知进度，不伪造工具成功", () => {
  const { messages, ids } = resumeSnapshot({ type: "resume_snapshot", history, user_message_id: 1, replay: false });
  assert.equal(ids, null);
  assert.equal(messages[1].incomplete, true);
  assert.deepEqual(messages[1].events, [{ type: "text_delta", text: "前文" }]);
});

test("订阅旧回合时事件不会写入后来开始的回合", () => {
  const later = { ...user, id: 3, parent_id: 2, content: "新问题" };
  const { messages, ids } = resumeSnapshot({ type: "resume_snapshot", history: { ...history, messages: [user, assistant, later] }, user_message_id: 1, replay: true });
  const current = applyChatEvent(messages, { type: "text_delta", text: "旧回答" }, ids, 2);
  assert.equal(current[1].events[0].text, "旧回答");
  assert.equal(current[2].text, "新问题");
});

test("恢复流接受error之后的收尾事件，只有stream_end完成", async () => {
  const result = [];
  for await (const event of readSse(stream(': heartbeat\n\nid: 1:1\ndata: {"type":"error","message":"正在收尾"}\n\nid: 1:2\ndata: {"type":"text_delta","text":"后续"}\n\ndata: {"type":"stream_end"}\n\n'), "resume")) result.push(event);
  assert.equal(result.length, 3);
  assert.equal(result[0].eventId, "1:1");
  await assert.rejects(async () => { for await (const _event of readSse(stream('data: {"type":"error","message":"收尾"}\n\n'), "resume")) {} }, /中断/);
});

test("204后校准历史，恢复失败只请求一次", async () => {
  const original = globalThis.fetch;
  const urls = [];
  try {
    globalThis.fetch = async (url) => { urls.push(url); return String(url).endsWith("/resume") ? new Response(null, { status: 204 }) : Response.json({ ...history, can_send_message: true }); };
    let calibrated;
    await recoverSession("session", new AbortController().signal, () => {}, (value) => { calibrated = value; });
    assert.equal(urls.length, 2);
    assert.equal(calibrated.can_send_message, true);
    urls.length = 0;
    globalThis.fetch = async (url) => { urls.push(url); throw new Error("offline"); };
    await assert.rejects(recoverSession("session", new AbortController().signal, () => {}, () => {}), /offline/);
    assert.equal(urls.length, 1);
  } finally { globalThis.fetch = original; }
});

test("恢复中取消不应用迟到历史，已存在会话404不解锁", async () => {
  const original = globalThis.fetch;
  try {
    globalThis.fetch = async () => new Response(null, { status: 404 });
    let called = false;
    await assert.rejects(recoverSession("session", new AbortController().signal, () => {}, () => { called = true; }), /404/);
    assert.equal(called, false);
    await recoverSession("session", new AbortController().signal, () => {}, () => { called = true; }, true);
    assert.equal(called, true);
  } finally { globalThis.fetch = original; }
});
