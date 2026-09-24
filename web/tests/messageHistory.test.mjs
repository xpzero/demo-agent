import assert from "node:assert/strict";
import { test } from "node:test";
import { compressionBoundaryIndex, historyToMessages, syncMessageTimestamps } from "../src/chat/messageHistory.ts";

test("历史消息按顺序恢复用户和助手文本", () => {
  const messages = historyToMessages([
    { id: 4, role: "user", content: "你好", created_at: 1700000000 },
    { id: 5, role: "assistant", content: "你好，有什么需要帮助？", created_at: 1700000005 },
    { id: 6, role: "user", content: "继续", created_at: 1700000010 },
  ]);
  assert.deepEqual(messages, [
    { kind: "user", id: "4", text: "你好", messageId: 4, createdAt: 1700000000 },
    { kind: "assistant", id: "5", createdAt: 1700000005, messageId: 5, events: [{ type: "text_delta", text: "你好，有什么需要帮助？" }] },
    { kind: "user", id: "6", text: "继续", messageId: 6, createdAt: 1700000010 },
  ]);
});

test("历史回复正文前展示该轮工具存档短文本", () => {
  const [assistant] = historyToMessages([
    { id: 9, role: "assistant", content: "今天晴", created_at: 1700000005,
      tool_runs: [{ id: 12, name: "get_weather", args_excerpt: "{'city': '北京'}", result_excerpt: "晴", duration_ms: 98 }] },
  ]);
  assert.deepEqual(assistant.events, [
    { type: "tool_call", id: "12", name: "get_weather", args: "{'city': '北京'}", excerpt: true },
    { type: "tool_result", id: "12", content: "晴", elapsed: 98 },
    { type: "text_delta", text: "今天晴" },
  ]);
});

test("空助手回复显示已完成，而不是持续思考中", () => {
  assert.deepEqual(historyToMessages([{ id: 8, role: "assistant", content: "", created_at: 1700000015 }]), [
    { kind: "assistant", id: "8", createdAt: 1700000015, messageId: 8, events: [{ type: "done", content: "", message_id: 8 }] },
  ]);
});

test("压缩分隔线只插在游标覆盖的最后一条原文后", () => {
  const messages = [
    { kind: "user", id: "a", messageId: 10 },
    { kind: "assistant", id: "b", messageId: 11 },
    { kind: "user", id: "c", messageId: 13 },
    { kind: "assistant", id: "pending" },
  ];
  assert.equal(compressionBoundaryIndex(messages, null), -1);
  assert.equal(compressionBoundaryIndex(messages, 11), 1);
  assert.equal(compressionBoundaryIndex(messages, 12), 1);
  assert.equal(compressionBoundaryIndex(messages.slice(0, 2), 11), -1);
});

test("校准实时消息时间时保留条目和流式事件", () => {
  const current = [
    { kind: "user", id: "local-user", messageId: 4, text: "你好", createdAt: 10 },
    { kind: "assistant", id: "local-assistant", messageId: 5, createdAt: 20, events: [
      { type: "text_delta", text: "回复" }, { type: "done", content: "回复", message_id: 5 },
    ] },
    { kind: "assistant", id: "pending", createdAt: 30, events: [] },
  ];
  const updated = syncMessageTimestamps(current, [
    { id: 4, created_at: 100 }, { id: 5, created_at: 200 },
  ]);
  assert.deepEqual(updated, [
    { ...current[0], createdAt: 100 },
    { ...current[1], createdAt: 200 },
    current[2],
  ]);
});
