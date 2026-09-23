import assert from "node:assert/strict";
import { test } from "node:test";
import { copyTextForEntry, historyToEntries, syncEntryTimestamps } from "../src/hooks/chatHistory.ts";

test("历史消息按顺序恢复用户和助手文本", () => {
  const entries = historyToEntries([
    { id: 4, role: "user", content: "你好", created_at: 1700000000 },
    { id: 5, role: "assistant", content: "你好，有什么需要帮助？", created_at: 1700000005 },
    { id: 6, role: "user", content: "继续", created_at: 1700000010 },
  ]);
  assert.deepEqual(entries, [
    { kind: "user", id: "4", text: "你好", messageId: 4, createdAt: 1700000000 },
    { kind: "assistant", id: "5", createdAt: 1700000005, messageId: 5, events: [{ type: "text_delta", text: "你好，有什么需要帮助？" }] },
    { kind: "user", id: "6", text: "继续", messageId: 6, createdAt: 1700000010 },
  ]);
});

test("空助手回复显示已完成，而不是持续思考中", () => {
  assert.deepEqual(historyToEntries([{ id: 8, role: "assistant", content: "", created_at: 1700000015 }]), [
    { kind: "assistant", id: "8", createdAt: 1700000015, messageId: 8, events: [{ type: "done", content: "", message_id: 8 }] },
  ]);
});

test("校准实时消息时间时保留条目和流式事件", () => {
  const entries = [
    { kind: "user", id: "local-user", messageId: 4, text: "你好", createdAt: 10 },
    { kind: "assistant", id: "local-assistant", messageId: 5, createdAt: 20, events: [
      { type: "text_delta", text: "回复" }, { type: "done", content: "回复", message_id: 5 },
    ] },
    { kind: "assistant", id: "pending", createdAt: 30, events: [] },
  ];
  const updated = syncEntryTimestamps(entries, [
    { id: 4, created_at: 100 }, { id: 5, created_at: 200 },
  ]);
  assert.deepEqual(updated, [
    { ...entries[0], createdAt: 100 },
    { ...entries[1], createdAt: 200 },
    entries[2],
  ]);
});

test("复制原始消息内容而非工具事件或状态文本", () => {
  assert.equal(copyTextForEntry({ kind: "user", id: "user", text: "消息\n正文", createdAt: 1 }), "消息\n正文");
  assert.equal(copyTextForEntry({ kind: "assistant", id: "assistant", createdAt: 2, events: [
    { type: "tool_call", id: "1", name: "search", args: {} },
    { type: "text_delta", text: "回" },
    { type: "text_delta", text: "复" },
    { type: "done", content: "回复", message_id: 5 },
  ] }), "回复");
  assert.equal(copyTextForEntry({ kind: "assistant", id: "pending", createdAt: 3, events: [] }), "");
});
