import assert from "node:assert/strict";
import { test } from "node:test";
import { historyToMessages, reconcileTurnFromHistory } from "../src/chat/messageHistory.ts";
import { appendPendingTurn, applyChatEvent } from "../src/chat/messageUpdates.ts";

const ids = { userId: "local-user", assistantId: "local-assistant" };

function streamedTurn(events, userMessageId = 7) {
  let messages = appendPendingTurn([], "查天气", ids, 10);
  for (const event of events) {
    messages = applyChatEvent(messages, event, ids, 11);
  }
  if (userMessageId !== null) {
    messages = applyChatEvent(messages, { type: "user_message", message_id: userMessageId }, ids, 12);
  }
  return messages;
}

test("历史 status=incomplete 的助手消息带未完成标记", () => {
  const [incomplete] = historyToMessages([
    { id: 8, role: "assistant", status: "incomplete", content: "部分回复", created_at: 1700000005 },
  ]);
  assert.equal(incomplete.incomplete, true);
  assert.deepEqual(incomplete.events, [{ type: "text_delta", text: "部分回复" }]);

  const [finished] = historyToMessages([
    { id: 9, role: "assistant", status: "finished", content: "完整回复", created_at: 1700000006 },
  ]);
  assert.equal(finished.incomplete, undefined);
});

test("空的未完成历史消息不再显示为空回复完成态", () => {
  const [emptyIncomplete] = historyToMessages([
    { id: 8, role: "assistant", status: "incomplete", content: "", created_at: 1700000005 },
  ]);
  assert.deepEqual(emptyIncomplete.events, []);
});

test("done 后用落库事实校准本轮正文与工具存档", () => {
  const current = streamedTurn([
    { type: "text_delta", text: "今天" },
    { type: "done", content: "今天", message_id: 8 },
  ]);
  const history = [
    { id: 7, parent_id: null, role: "user", status: "finished", content: "查天气", created_at: 100 },
    {
      id: 8, parent_id: 7, role: "assistant", status: "finished", content: "服务端最终正文", created_at: 101,
      tool_runs: [{ id: 12, name: "get_weather", args_excerpt: "{'city': '北京'}", result_excerpt: "晴", duration_ms: 98 }],
    },
  ];
  const next = reconcileTurnFromHistory(current, history, ids);
  assert.deepEqual(next[0], { kind: "user", id: "7", text: "查天气", createdAt: 100, messageId: 7 });
  assert.equal(next[1].messageId, 8);
  assert.equal(next[1].incomplete, undefined);
  assert.deepEqual(next[1].events.map((event) => event.type), ["tool_call", "tool_result", "text_delta"]);
  assert.equal(next[1].events.at(-1).text, "服务端最终正文");
});

test("error 终止后校准为未完成落库消息", () => {
  const current = streamedTurn([
    { type: "text_delta", text: "写到一半" },
    { type: "error", message: "上游中断" },
  ]);
  assert.equal(current[1].incomplete, true);
  const history = [
    { id: 7, parent_id: null, role: "user", status: "finished", content: "查天气", created_at: 100 },
    { id: 8, parent_id: 7, role: "assistant", status: "incomplete", content: "写到一半", created_at: 101 },
  ];
  const next = reconcileTurnFromHistory(current, history, ids);
  assert.equal(next[1].messageId, 8);
  assert.equal(next[1].incomplete, true);
});

test("本地 error 事件不覆盖已知落库消息的完成状态", () => {
  const current = streamedTurn([
    { type: "text_delta", text: "回复" },
    { type: "done", content: "回复", message_id: 8 },
    { type: "error", message: "连接断开" },
  ]);
  assert.equal(current[1].messageId, 8);
  assert.equal(current[1].incomplete, undefined);
});

test("同一父消息存在多条记录时优先取已完成的一条", () => {
  const current = streamedTurn([{ type: "error", message: "中断" }]);
  const history = [
    { id: 7, parent_id: null, role: "user", status: "finished", content: "查天气", created_at: 100 },
    { id: 8, parent_id: 7, role: "assistant", status: "incomplete", content: "残句", created_at: 101 },
    { id: 9, parent_id: 7, role: "assistant", status: "finished", content: "重试后的完整回复", created_at: 105 },
  ];
  const next = reconcileTurnFromHistory(current, history, ids);
  assert.equal(next[1].messageId, 9);
  assert.equal(next[1].incomplete, undefined);
});

test("落库事实缺失时保留本地展示，不伪造服务端消息", () => {
  const current = streamedTurn([{ type: "error", message: "中断" }]);
  assert.deepEqual(reconcileTurnFromHistory(current, [], ids), current);
  const noUser = streamedTurn([{ type: "error", message: "中断" }], null);
  assert.deepEqual(reconcileTurnFromHistory(noUser, [], ids), noUser);
  const noAssistant = [
    { kind: "user", id: ids.userId, text: "查天气", createdAt: 10, messageId: 7 },
  ];
  assert.deepEqual(reconcileTurnFromHistory(noAssistant, [
    { id: 7, parent_id: null, role: "user", status: "finished", content: "查天气", created_at: 100 },
  ], ids), noAssistant);
});
